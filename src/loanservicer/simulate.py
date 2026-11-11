"""Month-by-month servicing simulation with seeded borrower behavior.

Each month: ARM resets and forbearance changes on the 1st, then a day loop
(late fee assessment at the start of the day after grace, payments,
NSF returns, tax and insurance disbursements, collection calls), then month
end (buckets, roll rates, payment plans, loss mitigation referrals, escrow
analyses, remittance, reconciliation, invariants).
"""

from __future__ import annotations

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from .amortization import round_to_eighth
from .delinquency import BUCKETS, bucket, bucket_counts, days_past_due, months_behind, next_action, roll_rates
from .escrow import analyze, apply_analysis, tax_installment
from .invariants import check_loans
from .models import Loan
from .money import add_months, ceil_div, month_end, ym
from .reporting import delinquency_report, reconcile, remittance
from .servicing import Servicer
from .synth import POOLS, SIM_START, make_portfolio


@dataclass
class SimConfig:
    loans: int = 5000
    months: int = 24
    seed: int = 7
    start: date = SIM_START
    audit_path: str | None = None


def amount_due(loan: Loan, on: date) -> int:
    """What the monthly statement asks for: installments due through this month, plus fees, less held funds."""
    k = len(loan.unpaid_dues(date(on.year, on.month, 1)))
    if k == 0:
        return 0
    return max(0, loan.installments_total(k) + loan.late_fees - loan.suspense - loan.unapplied)


class Simulation:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        loans, index = make_portfolio(cfg.loans, cfg.seed, cfg.start)
        self.sv = Servicer(dict(POOLS), index, audit_path=cfg.audit_path)
        boarding = cfg.start - timedelta(days=1)
        for lo in loans:
            self.sv.board(lo, boarding)
        self.months: list[dict] = []
        self.totals: Counter = Counter()
        self.prev_buckets: dict[str, str] = {lo.loan_id: bucket(lo, boarding) for lo in loans}
        self.loan_index = {lo.loan_id: i for i, lo in enumerate(loans)}
        self.recast_month = {
            lo.loan_id: random.Random(cfg.seed * 17 + i).randint(2, max(2, cfg.months - 2))
            for i, lo in enumerate(loans)
            if lo.behavior == "recaster"
        }

    # ------------------------------------------------------------------
    def rng(self, loan: Loan, m: int, salt: int = 0) -> random.Random:
        return random.Random((self.cfg.seed * 1_000_003 + self.loan_index[loan.loan_id]) * 977 + m * 31 + salt)

    def run(self) -> dict:
        t0 = time.perf_counter()
        for m in range(self.cfg.months):
            self.step(m)
        self.elapsed = time.perf_counter() - t0
        return self.summary()

    def step(self, m: int) -> dict:
        sv = self.sv
        first = add_months(self.cfg.start, m)
        last = month_end(first)
        month = ym(first)
        c: Counter = Counter()
        active = [lo for lo in sv.loans.values() if lo.status == "active"]
        for pid in sv.pools:
            sv.upb_begin[(pid, month)] = sv.pool_upb(pid)

        # 1st of the month: forbearance and modifications, ARM resets, scheduled P&I
        for lo in active:
            fb = lo.forbearance
            if fb:
                if fb["status"] == "planned" and m == fb["start_month"]:
                    fb["status"] = "active"
                    lo.state = "forbearance"
                    lo.plan = None
                    lo.events.append({"on": first.isoformat(), "event": "forbearance_start", "months": 6})
                    c["forbearance_starts"] += 1
                elif fb["status"] in ("active", "mod_pending") and m == fb["end_month"]:
                    new_rate = max(2500, round_to_eighth(lo.rate_mp - 150))
                    sv.modify_loan(lo, first, new_rate, 480)
                    fb["status"] = "modified"
                    c["modifications"] += 1
            ev = sv.maybe_reset_arm(lo, first)
            if ev:
                c["arm_resets"] += 1
                if ev["limited_by"] != "none":
                    c[f"arm_limited_{ev['limited_by']}"] += 1
        sched: dict[str, Counter] = defaultdict(Counter)
        for lo in active:
            req = lo.installment_requirement()
            sched[lo.pool_id]["principal"] += req["principal"]
            sched[lo.pool_id]["interest"] += req["interest"]
            sched[lo.pool_id]["loans"] += 1
        for pid in sv.pools:
            sv.scheduled[(pid, month)] = dict(sched[pid])

        # build the month's events
        events: dict[int, list] = defaultdict(list)
        for lo in active:
            for day, prio, kind, payload in self.plan_month(lo, m, first, last):
                events[day].append((prio, lo.loan_id, kind, payload))
            if lo.escrowed:
                if first.month == lo.ins_month and lo.annual_ins:
                    events[10].append((0, lo.loan_id, "disburse", ("hazard insurance", lo.annual_ins)))
                if first.month in lo.tax_months and lo.annual_tax:
                    events[20].append((0, lo.loan_id, "disburse", ("property tax", tax_installment(lo, first.month))))
        fee_days: dict[int, list[str]] = defaultdict(list)
        for pid, pool in sv.pools.items():
            fee_days[pool.rules.grace_days + 2].append(pid)

        for day in range(1, last.day + 1):
            on = date(first.year, first.month, day)
            if day in fee_days:
                pids = set(fee_days[day])
                for lo in sv.loans.values():
                    if lo.pool_id in pids and lo.status == "active":
                        fee = sv.assess_late_fees(lo, on)
                        if fee:
                            c["late_fees_assessed"] += 1
                            c["late_fee_cents"] += fee
            todays = sorted(events.get(day, []), key=lambda e: (e[0], e[1]))
            for _prio, loan_id, kind, payload in todays:
                self.process(sv.loans[loan_id], on, kind, payload, events, c, m)
            if day in (18, 25):
                for lo in sv.loans.values():
                    if lo.status == "active" and days_past_due(lo, on) > 15:
                        action, _ = next_action(lo, on)
                        if action in ("call", "call_reminder", "call_offer_plan"):
                            lo.contact_log = [d for d in lo.contact_log if (on - d).days < 30] + [on]
                            c["collection_calls"] += 1

        # month end
        self.month_end(m, first, last, c)
        cur = {lo.loan_id: bucket(lo, last) for lo in sv.loans.values()}
        rr = roll_rates(self.prev_buckets, cur)
        self.prev_buckets = cur
        for pid in sv.pools:
            sv.upb_end[(pid, month)] = sv.pool_upb(pid)
        rem = {pid: remittance(sv, pid, month) for pid in sv.pools}
        rec = {pid: reconcile(sv, pid, month) for pid in sv.pools}
        checked, failures = check_loans(sv)
        for pid, r in rec.items():
            for ch in r["checks"]:
                if not ch["ok"]:
                    failures.append(f"{pid} {month}: {ch['check']} off by {ch['diff']}")
        sv.prune_snapshots(last)
        bc = bucket_counts(sv.loans.values(), last)
        result = {
            "month": month,
            "as_of": last.isoformat(),
            "active_loans": sum(bc["counts"].values()),
            "upb": sum(bc["upb"].values()),
            "buckets": bc["counts"],
            "bucket_upb": bc["upb"],
            "forbearance": sum(1 for lo in sv.loans.values() if lo.status == "active" and lo.state == "forbearance"),
            "roll": rr,
            "remittance": rem,
            "recon": rec,
            "ties_out": all(r["ties_out"] for r in rec.values()),
            "invariants": {"loans_checked": checked, "failures": failures[:50], "failure_count": len(failures)},
            "counts": dict(c),
        }
        self.months.append(result)
        self.totals.update(c)
        return result

    # ------------------------------------------------------------------
    def plan_month(self, lo: Loan, m: int, first: date, last: date) -> list[tuple]:
        rng = self.rng(lo, m)
        if lo.state == "forbearance":
            return []
        mb = months_behind(lo, first)
        hazard = 0.012 if lo.behavior == "prepayer" else 0.004
        if mb < 2 and lo.state in ("normal", "modified") and rng.random() < hazard:
            return [(rng.randint(10, 25), 1, "payoff", None)]
        if lo.state == "payment_plan" and lo.plan:
            if rng.random() < 0.8:
                return [(rng.randint(1, 15), 1, "fixed", lo.plan["amount"])]
            lo.plan["missed"] += 1
            return []
        if lo.state == "loss_mit":
            return [(rng.randint(5, 28), 1, "installments", 1)] if rng.random() < 0.3 else []
        b = lo.behavior
        r = rng.random()
        if b == "hardship":
            fb = lo.forbearance or {}
            if fb.get("status") == "planned" and m == fb.get("start_month", -9) - 1:
                return []
            return [] if r < 0.08 else [(rng.randint(1, 14), 1, "due", "manual")]
        if b in ("reliable", "nsf_prone"):
            if lo.autopay:
                nsf_p = 0.15 if b == "nsf_prone" else 0.002
                day = 1 + self.loan_index[lo.loan_id] % 5
                return [(day, 1, "due", "autopay_nsf" if rng.random() < nsf_p else "autopay")]
            return [] if r < 0.005 else [(rng.randint(1, 14), 1, "due", "manual")]
        if b == "manual_ontime":
            if r < 0.01:
                return []
            if r < 0.03:
                return [(rng.randint(17, 27), 1, "due", "manual")]
            return [(rng.randint(1, 12), 1, "due", "manual")]
        if b == "sometimes_late":
            if r < 0.05:
                return []
            if r < 0.50:
                return [(rng.randint(17, 28), 1, "due", "manual")]
            return [(rng.randint(3, 15), 1, "due", "manual")]
        if b == "partial":
            if r < 0.25:
                return [(rng.randint(5, 25), 1, "due", "manual")]
            return [(rng.randint(5, 25), 1, "fixed", lo.monthly_payment() * rng.randint(50, 90) // 100)]
        if b == "chronic":
            if r < 0.45:
                return []
            if r < 0.60 and mb >= 1:
                return [(rng.randint(5, 28), 1, "installments", 2)]
            return [(rng.randint(5, 28), 1, "installments", 1)]
        if b == "prepayer":
            return [(rng.randint(1, 10), 1, "extra", rng.randint(100, 1000) * 100)]
        if b == "recaster":
            if m == self.recast_month.get(lo.loan_id) and not lo.in_io():
                return [(rng.randint(2, 10), 1, "curtail", rng.randint(15, 30))]
            return [(rng.randint(1, 12), 1, "due", "manual")]
        return [(rng.randint(1, 10), 1, "due", "manual")]

    def process(self, lo: Loan, on: date, kind: str, payload, events, c: Counter, m: int) -> None:
        sv = self.sv
        if lo.status != "active":
            return
        if kind == "disburse":
            sv.disburse(lo, payload[0], payload[1], on)
            c["disbursements"] += 1
            return
        if kind == "payoff":
            sv.process_payoff(lo, on)
            c["payoffs"] += 1
            return
        if kind == "reverse":
            t = next((x for x in lo.history if x.txn_id == payload), None)
            if t is None or t.reversed_by:
                return
            sv.reverse_payment(lo.loan_id, payload, on, "NSF")
            c["nsf_reversals"] += 1
            if self.rng(lo, m, 5).random() < 0.75 and on.day < 26:
                events[min(on.day + self.rng(lo, m, 6).randint(4, 12), 28)].append((1, lo.loan_id, "due", "manual"))
            return
        if kind == "recast":
            if not lo.in_io() and lo.principal > 0:
                sv.recast(lo, on)
                c["recasts"] += 1
            return
        method = "manual"
        if kind == "due":
            method = "autopay" if payload.startswith("autopay") else "manual"
            amount = amount_due(lo, on)
        elif kind == "fixed":
            amount = payload
        elif kind == "installments":
            k = len(lo.unpaid_dues(date(on.year, on.month, 1)))
            amount = lo.installments_total(min(payload, max(k, 1))) + lo.late_fees - lo.suspense - lo.unapplied
        elif kind == "extra":
            base = amount_due(lo, on)
            amount = base + payload if base > 0 else 0
            c["extra_principal_payments"] += 1 if amount else 0
        elif kind == "curtail":
            amount = amount_due(lo, on) + lo.principal * payload // 100
            events[on.day + 5].append((3, lo.loan_id, "recast", None))
            c["large_curtailments"] += 1
        else:
            raise ValueError(kind)
        if amount <= 0:
            return
        t = sv.apply_payment(lo.loan_id, amount, on, method)
        c["payments"] += 1
        c[f"payments_{method}"] += 1
        if t.splits.get("suspense", 0) > 0:
            c["payments_to_suspense"] += 1
        if t.splits.get("curtailment", 0) > 0:
            c["curtailments"] += 1
        if kind == "due" and payload == "autopay_nsf":
            events[on.day + 4].append((2, lo.loan_id, "reverse", t.txn_id))

    def month_end(self, m: int, first: date, last: date, c: Counter) -> None:
        sv = self.sv
        nxt = add_months(first, 1)
        for lo in sv.loans.values():
            if lo.status != "active":
                continue
            mb = months_behind(lo, last)
            if lo.state == "payment_plan" and lo.plan:
                p = lo.plan
                if mb == 0:
                    p["status"] = "completed"
                    lo.state = "normal"
                    c["plans_completed"] += 1
                elif p["missed"] or m >= p["start"] + p["months"] - 1:
                    p["status"] = "broken"
                    lo.state = "normal"
                    c["plans_broken"] += 1
            if lo.state in ("normal", "modified", "payment_plan") and mb >= 3:
                lo.state = "loss_mit"
                lo.plan = None
                lo.events.append({"on": last.isoformat(), "event": "loss_mit_referral", "months_behind": mb})
                c["loss_mit_referrals"] += 1
            elif lo.state == "loss_mit" and lo.forbearance is None and self.rng(lo, m, 9).random() < 0.12:
                lo.forbearance = {"status": "mod_pending", "end_month": m + 1}
            elif (
                lo.state in ("normal", "modified")
                and mb == 2
                and not lo.borrower.bankruptcy
                and (lo.plan is None or lo.plan["status"] != "broken")
                and self.rng(lo, m, 7).random() < 0.5
            ):
                arrears = amount_due(lo, last)
                lo.plan = {
                    "status": "active",
                    "start": m + 1,
                    "months": 3,
                    "missed": 0,
                    "amount": lo.installments_total(1) + ceil_div(arrears, 3),
                }
                lo.state = "payment_plan"
                lo.events.append({"on": last.isoformat(), "event": "payment_plan", "amount": lo.plan["amount"]})
                c["plans_offered"] += 1
            if lo.escrowed and lo.analysis_month == nxt.month:
                rng = self.rng(lo, m, 11)
                lo.annual_tax = lo.annual_tax * (100 + rng.randint(-3, 6)) // 100
                lo.annual_ins = lo.annual_ins * (100 + rng.randint(-8, 14)) // 100
                res = analyze(lo, nxt, sv.rules(lo))
                apply_analysis(sv, lo, res, last)
                c["escrow_analyses"] += 1
                if res["shortage"]:
                    c["escrow_shortages"] += 1
                if res["refund"]:
                    c["escrow_refunds"] += 1

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        months = self.months
        if not months:
            return {}
        avg = {}
        pairs = [("current", "30"), ("30", "60"), ("60", "90"), ("90", "120+"), ("30", "current"), ("60", "current")]
        for a, b in pairs:
            num = sum(mo["roll"]["counts"][a][b] for mo in months)
            den = sum(sum(mo["roll"]["counts"][a].values()) for mo in months)
            avg[f"{a}->{b}"] = (num * 10_000 + den // 2) // den if den else 0
        last = months[-1]
        return {
            "loans": self.cfg.loans,
            "months": len(months),
            "seed": self.cfg.seed,
            "first_month": months[0]["month"],
            "last_month": last["month"],
            "active_loans_end": last["active_loans"],
            "upb_end": last["upb"],
            "buckets_end": last["buckets"],
            "roll_rates_avg_bp": avg,
            "counts": dict(self.totals),
            "all_invariants_hold": all(mo["invariants"]["failure_count"] == 0 for mo in months),
            "invariant_failures": sum(mo["invariants"]["failure_count"] for mo in months),
            "loan_checks": sum(mo["invariants"]["loans_checked"] for mo in months),
            "remittance_ties_out": all(mo["ties_out"] for mo in months),
            "elapsed_seconds": round(getattr(self, "elapsed", 0.0), 2),
            "delinquency_by_pool": delinquency_report(self.sv, date.fromisoformat(last["as_of"])),
        }


def series(sim: Simulation) -> dict:
    """Month-by-month delinquency shares in basis points of active loans, for charts."""
    out = {b: [] for b in BUCKETS[1:]}
    months = []
    for mo in sim.months:
        months.append(mo["month"])
        n = mo["active_loans"] or 1
        for b in BUCKETS[1:]:
            out[b].append((mo["buckets"][b] * 10_000 + n // 2) // n)
    return {"months": months, "bp": out}
