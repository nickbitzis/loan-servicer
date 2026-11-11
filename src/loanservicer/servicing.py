"""The servicing engine: payment waterfall, suspense, NSF reversal, late fees,
escrow disbursements, ARM resets, recast, modification and payoff.

Every state change that moves money posts a balanced ledger entry and appends
a transaction to the loan's history.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from .amortization import arm_reset, is_reset_installment, level_payment, modify, recast_payment
from .ledger import Ledger, payment_lines
from .models import CONTRACTUAL, Loan, Pool, Txn
from .money import add_months, div_round, month_index, monthly_interest, require_cents, ym


class ServicingError(Exception):
    pass


REVERSAL_WINDOW_DAYS = 60


def _copy_inst(st: dict | None) -> dict | None:
    if st is None:
        return None
    return {"due": st["due"], "req": dict(st["req"]), "paid": dict(st["paid"])}


class Servicer:
    def __init__(self, pools: dict[str, Pool], index: dict[int, int], audit_path: str | Path | None = None):
        self.pools = pools
        self.index = index
        self.loans: dict[str, Loan] = {}
        self.ledger = Ledger()
        self.seq = 0
        self.audit: list[dict] = []
        self.audit_path = Path(audit_path) if audit_path else None
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # month bookkeeping for investor reports
        self.upb_begin: dict[tuple[str, str], int] = {}
        self.upb_end: dict[tuple[str, str], int] = {}
        self.scheduled: dict[tuple[str, str], dict[str, int]] = {}
        self.by_month: dict[tuple[str, str], list[Txn]] = defaultdict(list)

    # basics -------------------------------------------------------------
    def rules(self, loan: Loan):
        return self.pools[loan.pool_id].rules

    def get(self, loan_id: str) -> Loan:
        loan = self.loans.get(loan_id)
        if loan is None:
            raise KeyError(loan_id)
        return loan

    def _next_id(self) -> str:
        self.seq += 1
        return f"T{self.seq:08d}"

    def _txn(self, loan: Loan, on: date, kind: str, amount: int, **kw) -> Txn:
        t = Txn(self._next_id(), loan.loan_id, loan.pool_id, on, kind, amount, **kw)
        loan.history.append(t)
        self.by_month[(loan.pool_id, ym(on))].append(t)
        return t

    def record_audit(self, action: str, **fields) -> None:
        entry = {"seq": len(self.audit) + 1, "action": action, **fields}
        self.audit.append(entry)
        if self.audit_path:
            with self.audit_path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")

    def board(self, loan: Loan, on: date) -> None:
        """Add a loan to the book with its opening principal and escrow balances."""
        self.loans[loan.loan_id] = loan
        self.ledger.post(
            loan.loan_id, loan.pool_id, on, {"loan_upb": loan.principal, "investor_funding": -loan.principal}
        )
        if loan.escrow_balance:
            self.ledger.post(
                loan.loan_id,
                loan.pool_id,
                on,
                {"custodial_cash": loan.escrow_balance, "escrow": -loan.escrow_balance},
            )

    # payment waterfall --------------------------------------------------
    def apply_payment(self, loan_id: str, amount: int, on: date, method: str = "manual", note: str = "") -> Txn:
        loan = self.get(loan_id)
        require_cents(amount)
        if amount <= 0:
            raise ServicingError("payment amount must be positive")
        if loan.status != "active":
            raise ServicingError(f"loan {loan_id} is {loan.status}")
        rules = self.rules(loan)
        before = {
            "suspense": loan.suspense,
            "unapplied": loan.unapplied,
            "inst_state": _copy_inst(loan.inst_state),
            "next_due": loan.next_due,
            "installments_paid": loan.installments_paid,
            "remaining_term": loan.remaining_term,
            "pi_payment": loan.pi_payment,
            "schedule_version": loan.schedule_version,
        }
        released_s, released_u = loan.suspense, loan.unapplied
        pool = amount + released_s + released_u
        loan.suspense = loan.unapplied = 0
        s: Counter = Counter()
        installments: list[str] = []
        limit = date(on.year, on.month, 1)
        if rules.pay_ahead:
            limit = add_months(limit, 1)
        order = rules.order
        first_contract = min(order.index(c) for c in CONTRACTUAL)
        fees_first = order.index("late_fees") < first_contract
        stopped_short = False

        while loan.next_due <= limit and loan.remaining_term > 0 and pool > 0:
            st = loan.current_installment()
            rem = loan.installment_remaining()
            need = sum(rem.values())
            if loan.next_due > on and pool < need + (loan.late_fees if fees_first else 0):
                break  # not yet due and not fully covered: leave the money for the excess rule
            if rules.partial_policy == "suspense":
                pre_fees = min(pool, loan.late_fees) if fees_first else 0
                if pool - pre_fees < need:
                    stopped_short = True
                    break
            for comp in order:
                if pool == 0:
                    break
                if comp == "late_fees":
                    take = min(pool, loan.late_fees)
                    loan.late_fees -= take
                    s["late_fees"] += take
                    pool -= take
                    continue
                take = min(pool, rem[comp])
                if take == 0:
                    continue
                self._apply_component(loan, comp, take, s, st["req"], rules)
                st["paid"][comp] += take
                rem[comp] -= take
                pool -= take
            if any(rem[c] for c in CONTRACTUAL):
                stopped_short = True
                break
            installments.append(loan.next_due.isoformat())
            self._satisfy(loan, on)

        if pool and loan.late_fees and not stopped_short:
            take = min(pool, loan.late_fees)
            loan.late_fees -= take
            s["late_fees"] += take
            pool -= take
        if pool and stopped_short and rules.partial_policy == "suspense":
            loan.suspense = pool
            pool = 0
        if pool and rules.excess == "curtailment" and loan.principal > 0 and not stopped_short:
            if loan.inst_state is not None and any(loan.inst_state["paid"].values()):
                room = loan.principal - loan.installment_remaining()["principal"]
            else:
                loan.inst_state = None
                room = loan.principal
            take = min(pool, room)
            loan.principal -= take
            s["curtailment"] += take
            pool -= take
        if pool:
            loan.unapplied = pool
            pool = 0

        s["suspense"] = loan.suspense - released_s
        s["unapplied"] = loan.unapplied - released_u
        splits = {k: v for k, v in s.items() if v}
        t = self._txn(
            loan,
            on,
            "payment",
            amount,
            method=method,
            splits=splits,
            installments=installments,
            note=note,
            before=before,
        )
        self._check_split_sum(t)
        self.ledger.post(loan.loan_id, loan.pool_id, on, payment_lines(amount, splits))
        return t

    def _apply_component(self, loan: Loan, comp: str, take: int, s: Counter, req: dict, rules) -> None:
        s[comp] += take
        if comp == "principal":
            loan.principal -= take
        elif comp == "escrow":
            loan.escrow_balance += take
        elif comp == "escrow_shortage":
            loan.escrow_balance += take
            # an escrow analysis can lower the shortage while an installment is part paid
            loan.shortage_outstanding = max(0, loan.shortage_outstanding - take)
        elif comp == "interest" and req["_rate"] > 0:
            s["servicing_fee"] += div_round(take * rules.servicing_fee_mp, req["_rate"])
            s["gfee"] += div_round(take * rules.gfee_mp, req["_rate"])

    def _satisfy(self, loan: Loan, on: date) -> None:
        loan.next_due = add_months(loan.next_due, 1)
        loan.installments_paid += 1
        loan.remaining_term -= 1
        loan.inst_state = None
        if loan.io_months and loan.installments_paid == loan.io_months and loan.remaining_term > 0:
            loan.pi_payment = level_payment(loan.principal, loan.rate_mp, loan.remaining_term)
            if not any(e["event"] == "io_end" for e in loan.events):
                loan.events.append({"on": on.isoformat(), "event": "io_end", "new_payment": loan.pi_payment})

    @staticmethod
    def _check_split_sum(t: Txn) -> None:
        s = t.splits
        applied = sum(
            s.get(k, 0)
            for k in ("late_fees", "escrow_shortage", "interest", "principal", "escrow", "curtailment", "payoff_fees")
        )
        if applied + s.get("suspense", 0) + s.get("unapplied", 0) != t.amount:
            raise ServicingError(f"split sum mismatch on {t.txn_id}: {s} vs {t.amount}")

    # NSF reversal -------------------------------------------------------
    def reverse_payment(self, loan_id: str, txn_id: str, on: date, reason: str = "NSF", fee: bool = True) -> dict:
        """Reverse a returned payment. Later payments are unwound, the returned
        one is reversed, and the later ones are applied again in order."""
        loan = self.get(loan_id)
        idx = next((i for i, t in enumerate(loan.history) if t.txn_id == txn_id), None)
        if idx is None:
            raise ServicingError(f"no transaction {txn_id} on {loan_id}")
        target = loan.history[idx]
        if target.kind != "payment" or target.reversed_by:
            raise ServicingError(f"{txn_id} is not an open payment")
        if target.before is None or (on - target.on).days > REVERSAL_WINDOW_DAYS:
            raise ServicingError(f"{txn_id} is outside the {REVERSAL_WINDOW_DAYS} day reversal window")
        if loan.status != "active":
            raise ServicingError("cannot reverse a payment on a paid off loan")
        if loan.schedule_version != target.before["schedule_version"]:
            raise ServicingError("cannot unwind across a recast, modification or rate reset")
        later = [t for t in loan.history[idx + 1 :] if t.kind == "payment" and not t.reversed_by]
        unwound = []
        for t in reversed(later):
            unwound.append(self._unwind(loan, t, on, f"unwound to reverse {txn_id}").txn_id)
        rev = self._unwind(loan, target, on, reason)
        rules = self.rules(loan)
        if fee and rules.nsf_fee:
            loan.late_fees += rules.nsf_fee
            self._txn(loan, on, "fee_assessed", 0, splits={"nsf_fee": rules.nsf_fee}, note=f"NSF fee for {txn_id}")
        if reason == "NSF":
            loan.nsf_count += 1
        reapplied = [
            self.apply_payment(loan_id, t.amount, on, t.method, note=f"reapplied after reversal of {txn_id}").txn_id
            for t in later
        ]
        return {"reversal": rev.txn_id, "unwound": unwound, "reapplied": reapplied}

    def _unwind(self, loan: Loan, t: Txn, on: date, note: str) -> Txn:
        s = t.splits
        loan.principal += s.get("principal", 0) + s.get("curtailment", 0)
        loan.escrow_balance -= s.get("escrow", 0) + s.get("escrow_shortage", 0)
        loan.shortage_outstanding += s.get("escrow_shortage", 0)
        loan.late_fees += s.get("late_fees", 0)
        b = t.before
        loan.suspense = b["suspense"]
        loan.unapplied = b["unapplied"]
        loan.inst_state = _copy_inst(b["inst_state"])
        loan.next_due = b["next_due"]
        loan.installments_paid = b["installments_paid"]
        loan.remaining_term = b["remaining_term"]
        loan.pi_payment = b["pi_payment"]
        neg = {k: -v for k, v in s.items()}
        rev = self._txn(loan, on, "reversal", -t.amount, method=t.method, splits=neg, note=note, reverses=t.txn_id)
        t.reversed_by = rev.txn_id
        t.before = None
        self.ledger.post(loan.loan_id, loan.pool_id, on, payment_lines(-t.amount, neg))
        return rev

    # fees ---------------------------------------------------------------
    def assess_late_fees(self, loan: Loan, on: date) -> int:
        """Assess one late fee per installment still unpaid after the grace period.

        Run at the start of a day: an installment due on the 1st with a 15 day
        grace period is late if it is still unpaid when day 17 begins.
        """
        if loan.status != "active" or loan.state in ("forbearance", "payment_plan"):
            return 0
        rules = self.rules(loan)
        total = 0
        for d in loan.unpaid_dues(on):
            if (on - d).days <= rules.grace_days or d in loan.late_fee_assessed:
                continue
            req = loan.current_installment()["req"] if d == loan.next_due else loan.installment_requirement()
            fee = min(div_round((req["interest"] + req["principal"]) * rules.late_fee_bp, 10_000), rules.late_fee_cap)
            loan.late_fee_assessed.add(d)
            loan.late_fees += fee
            total += fee
            self._txn(loan, on, "fee_assessed", 0, splits={"late_fee": fee}, note=f"late fee for {d.isoformat()}")
        return total

    # escrow -------------------------------------------------------------
    def disburse(self, loan: Loan, kind: str, amount: int, on: date) -> Txn:
        require_cents(amount)
        loan.escrow_balance -= amount
        t = self._txn(loan, on, "disbursement", -amount, splits={"escrow": -amount}, note=kind)
        self.ledger.post(loan.loan_id, loan.pool_id, on, {"escrow": amount, "custodial_cash": -amount})
        return t

    def refund_escrow(self, loan: Loan, amount: int, on: date, note: str) -> Txn | None:
        if amount <= 0:
            return None
        loan.escrow_balance -= amount
        t = self._txn(loan, on, "escrow_refund", -amount, splits={"escrow": -amount}, note=note)
        self.ledger.post(loan.loan_id, loan.pool_id, on, {"escrow": amount, "custodial_cash": -amount})
        return t

    # rate and schedule changes ------------------------------------------
    def maybe_reset_arm(self, loan: Loan, on: date) -> dict | None:
        """On the 1st of a month: if this is a reset date, re-price and re-amortize."""
        if loan.arm is None or loan.status != "active" or loan.state == "modified":
            return None
        n = month_index(on) - month_index(loan.first_due) + 1
        if not is_reset_installment(loan.arm, n):
            return None
        idx = self.index[month_index(on) - 2]  # 45 day lookback, approximated as two months
        res = arm_reset(loan.arm, idx, loan.rate_mp, loan.orig_rate_mp, first_reset=loan.resets_done == 0)
        old_payment = loan.pi_payment
        loan.rate_mp = res.new_rate_mp
        loan.resets_done += 1
        io_left = max(0, loan.io_months - loan.installments_paid)
        if io_left == 0 and loan.remaining_term > 0:
            loan.pi_payment = level_payment(loan.principal, loan.rate_mp, loan.remaining_term)
        if loan.inst_state is not None and not any(loan.inst_state["paid"].values()):
            loan.inst_state = None
        loan.schedule_version += 1
        ev = {
            "on": on.isoformat(),
            "event": "arm_reset",
            "index_mp": res.index_mp,
            "fully_indexed_mp": res.fully_indexed_mp,
            "old_rate_mp": res.old_rate_mp,
            "new_rate_mp": res.new_rate_mp,
            "limited_by": res.limited_by,
            "old_payment": old_payment,
            "new_payment": loan.pi_payment,
        }
        loan.events.append(ev)
        return ev

    def recast(self, loan: Loan, on: date) -> dict:
        if loan.in_io():
            raise ServicingError("recast is not available during the interest only period")
        if loan.principal <= 0 or loan.remaining_term <= 0:
            raise ServicingError("nothing to recast")
        old = loan.pi_payment
        loan.pi_payment = recast_payment(loan.principal, loan.rate_mp, loan.remaining_term)
        if loan.inst_state is not None and not any(loan.inst_state["paid"].values()):
            loan.inst_state = None
        loan.schedule_version += 1
        ev = {
            "on": on.isoformat(),
            "event": "recast",
            "balance": loan.principal,
            "remaining_term": loan.remaining_term,
            "old_payment": old,
            "new_payment": loan.pi_payment,
        }
        loan.events.append(ev)
        self.record_audit("recast", loan_id=loan.loan_id, **ev)
        return ev

    def modify_loan(self, loan: Loan, effective: date, new_rate_mp: int, new_term: int) -> dict:
        """Rate and term modification. Delinquent interest and any escrow advance
        are capitalized, late fees are waived, and the loan re-amortizes."""
        behind = [d for d in loan.unpaid_dues(effective) if d < effective]
        cap_interest = monthly_interest(loan.principal, loan.rate_mp) * len(behind)
        st = loan.inst_state
        if behind and st is not None and st["due"] == loan.next_due:
            cap_interest -= st["paid"]["interest"]
        cap_escrow = max(0, -loan.escrow_balance)
        mod = modify(loan.principal, loan.pi_payment, cap_interest + cap_escrow, new_rate_mp, new_term)
        if loan.late_fees:
            self._txn(
                loan, effective, "fee_waived", 0, splits={"late_fee": -loan.late_fees}, note="waived at modification"
            )
            loan.late_fees = 0
        loan.principal = mod.new_balance
        loan.escrow_balance += cap_escrow
        loan.shortage_outstanding = 0
        loan.shortage_monthly = 0
        loan.rate_mp = new_rate_mp
        loan.pi_payment = mod.new_payment
        loan.remaining_term = new_term
        loan.io_months = 0
        loan.arm = None
        loan.next_due = effective
        loan.inst_state = None
        loan.state = "modified"
        loan.schedule_version += 1
        self._txn(
            loan,
            effective,
            "capitalization",
            0,
            splits={"capitalized_interest": cap_interest, "capitalized_escrow": cap_escrow},
            note=f"modification: {len(behind)} installments of arrears capitalized",
        )
        self.ledger.post(
            loan.loan_id,
            loan.pool_id,
            effective,
            {"loan_upb": mod.capitalized, "capitalized_interest": -cap_interest, "escrow": -cap_escrow},
        )
        ev = {"on": effective.isoformat(), "event": "modification", **mod.to_dict()}
        loan.events.append(ev)
        self.record_audit("modification", loan_id=loan.loan_id, **ev)
        return ev

    # payoff -------------------------------------------------------------
    def payoff_quote(self, loan: Loan, payoff_date: date) -> dict:
        if loan.status != "active":
            raise ServicingError(f"loan {loan.loan_id} is {loan.status}")
        rules = self.rules(loan)
        accrual_start = add_months(loan.next_due, -1)
        days = max(0, (payoff_date - accrual_start).days)
        denom = 100_000 * rules.day_count
        interest = div_round(loan.principal * loan.rate_mp * days, denom)
        st = loan.inst_state
        paid_int = st["paid"]["interest"] if st is not None and st["due"] == loan.next_due else 0
        interest = max(0, interest - paid_int)
        per_diem = div_round(loan.principal * loan.rate_mp, denom)
        advance = max(0, -loan.escrow_balance)
        credits = loan.suspense + loan.unapplied
        total = loan.principal + interest + loan.late_fees + rules.release_fee + advance - credits
        return {
            "loan_id": loan.loan_id,
            "payoff_date": payoff_date.isoformat(),
            "good_through": payoff_date.isoformat(),
            "principal": loan.principal,
            "interest_from": accrual_start.isoformat(),
            "interest_days": days,
            "day_count": rules.day_count,
            "rate_mp": loan.rate_mp,
            "per_diem": per_diem,
            "interest": interest,
            "late_fees": loan.late_fees,
            "release_fee": rules.release_fee,
            "escrow_advance": advance,
            "suspense_credit": loan.suspense,
            "unapplied_credit": loan.unapplied,
            "total": total,
            "escrow_refund_after_payoff": max(0, loan.escrow_balance),
            "notes": [
                "Interest accrues daily on the principal from the first day of the unpaid interest period.",
                f"Each day after the good-through date adds {per_diem} cents of interest.",
                "The escrow balance is refunded to the borrower after payoff and is not netted against this total.",
            ],
            "per_day_after": [
                {
                    "date": (payoff_date + timedelta(days=i)).isoformat(),
                    "total": total
                    - interest
                    + max(0, div_round(loan.principal * loan.rate_mp * (days + i), denom) - paid_int),
                }
                for i in range(1, 4)
            ],
        }

    def process_payoff(self, loan: Loan, on: date, method: str = "wire") -> Txn:
        q = self.payoff_quote(loan, on)
        rules = self.rules(loan)
        rate = loan.rate_mp
        s = {
            "principal": q["principal"],
            "interest": q["interest"],
            "servicing_fee": div_round(q["interest"] * rules.servicing_fee_mp, rate) if rate else 0,
            "gfee": div_round(q["interest"] * rules.gfee_mp, rate) if rate else 0,
            "late_fees": q["late_fees"],
            "payoff_fees": q["release_fee"],
            "escrow": q["escrow_advance"],
            "suspense": -loan.suspense,
            "unapplied": -loan.unapplied,
        }
        s = {k: v for k, v in s.items() if v}
        loan.principal = 0
        loan.late_fees = 0
        loan.suspense = 0
        loan.unapplied = 0
        loan.escrow_balance += q["escrow_advance"]
        loan.shortage_outstanding = 0
        loan.shortage_monthly = 0
        loan.inst_state = None
        loan.remaining_term = 0
        loan.status = "paid_off"
        t = self._txn(loan, on, "payoff", q["total"], method=method, splits=s, note="paid in full")
        self._check_split_sum(t)
        self.ledger.post(loan.loan_id, loan.pool_id, on, payment_lines(q["total"], s))
        self.refund_escrow(loan, loan.escrow_balance, on, "escrow refund after payoff")
        loan.events.append({"on": on.isoformat(), "event": "payoff", "amount": q["total"]})
        return t

    # helpers for reports -------------------------------------------------
    def pool_upb(self, pool_id: str) -> int:
        return sum(lo.principal for lo in self.loans.values() if lo.pool_id == pool_id)

    def prune_snapshots(self, as_of: date) -> None:
        cutoff = as_of - timedelta(days=REVERSAL_WINDOW_DAYS)
        for loan in self.loans.values():
            for t in loan.history:
                if t.before is not None and t.on < cutoff:
                    t.before = None
