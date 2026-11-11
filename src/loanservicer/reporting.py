"""Investor reporting: monthly remittance per pool and the tie-out to the ledger.

Remittance here is actual/actual: the servicer passes through what it
collected. Scheduled principal and interest are shown next to actuals so the
investor sees the gap (what a scheduled/scheduled servicer would advance).
"""

from __future__ import annotations

from collections import Counter

from .delinquency import BUCKETS, bucket_counts
from .servicing import Servicer

CASH_KINDS = ("payment", "reversal", "payoff")


def loan_level_totals(sv: Servicer, pool_id: str, month: str) -> dict[str, int]:
    """Sum transaction splits for one pool and month, straight from loan histories."""
    t: Counter = Counter()
    for x in sv.by_month.get((pool_id, month), []):
        s = x.splits
        if x.kind in CASH_KINDS:
            t["cash_received"] += x.amount
            if x.kind == "payoff":
                t["payoff_principal"] += s.get("principal", 0)
                t["payoffs"] += 1
            else:
                t["scheduled_principal_collected"] += s.get("principal", 0)
            t["curtailment"] += s.get("curtailment", 0)
            t["interest"] += s.get("interest", 0)
            t["servicing_fee"] += s.get("servicing_fee", 0)
            t["gfee"] += s.get("gfee", 0)
            t["late_fees"] += s.get("late_fees", 0)
            t["payoff_fees"] += s.get("payoff_fees", 0)
            t["escrow_in"] += s.get("escrow", 0) + s.get("escrow_shortage", 0)
            t["suspense_change"] += s.get("suspense", 0)
            t["unapplied_change"] += s.get("unapplied", 0)
        elif x.kind in ("disbursement", "escrow_refund"):
            t["escrow_out"] += -x.amount
        elif x.kind == "capitalization":
            t["capitalized"] += s.get("capitalized_interest", 0) + s.get("capitalized_escrow", 0)
    t["principal_collected"] = t["scheduled_principal_collected"] + t["curtailment"] + t["payoff_principal"]
    t["net_interest"] = t["interest"] - t["servicing_fee"] - t["gfee"]
    return dict(t)


def remittance(sv: Servicer, pool_id: str, month: str) -> dict:
    t = loan_level_totals(sv, pool_id, month)
    sched = sv.scheduled.get((pool_id, month), {})
    pool = sv.pools[pool_id]
    sp, si = sched.get("principal", 0), sched.get("interest", 0)
    collected_pi = t.get("scheduled_principal_collected", 0) + t.get("interest", 0)
    return {
        "pool_id": pool_id,
        "pool_name": pool.name,
        "month": month,
        "loans_scheduled": sched.get("loans", 0),
        "scheduled_principal": sp,
        "scheduled_interest": si,
        "actual_scheduled_principal": t.get("scheduled_principal_collected", 0),
        "curtailments": t.get("curtailment", 0),
        "payoff_principal": t.get("payoff_principal", 0),
        "payoffs": t.get("payoffs", 0),
        "actual_principal": t.get("principal_collected", 0),
        "actual_interest": t.get("interest", 0),
        "servicing_fee": t.get("servicing_fee", 0),
        "gfee": t.get("gfee", 0),
        "net_interest": t.get("net_interest", 0),
        "remittance": t.get("principal_collected", 0) + t.get("net_interest", 0),
        "scheduled_minus_collected_pi": sp + si - collected_pi,
        "servicing_fee_mp": pool.rules.servicing_fee_mp,
        "gfee_mp": pool.rules.gfee_mp,
    }


def _check(name: str, expected: int, actual: int) -> dict:
    return {"check": name, "expected": expected, "actual": actual, "diff": actual - expected, "ok": expected == actual}


def reconcile(sv: Servicer, pool_id: str, month: str) -> dict:
    """Tie the loan-level transactions to the ledger and the pool's UPB roll-forward."""
    t = loan_level_totals(sv, pool_id, month)
    led = sv.ledger
    g = lambda acct: led.pool_month(pool_id, month, acct)  # noqa: E731
    begin = sv.upb_begin.get((pool_id, month))
    end = sv.upb_end.get((pool_id, month))
    if end is None:
        end = sv.pool_upb(pool_id)
    rem = remittance(sv, pool_id, month)
    checks = [
        _check(
            "principal: loan level vs ledger loan_upb",
            t.get("principal_collected", 0) - t.get("capitalized", 0),
            -g("loan_upb"),
        ),
        _check(
            "net interest: loan level vs ledger investor_interest", t.get("net_interest", 0), -g("investor_interest")
        ),
        _check("servicing fee: loan level vs ledger", t.get("servicing_fee", 0), -g("servicing_fee")),
        _check("guarantee fee: loan level vs ledger", t.get("gfee", 0), -g("gfee_payable")),
        _check(
            "custodial cash: loan level vs ledger",
            t.get("cash_received", 0) - t.get("escrow_out", 0),
            g("custodial_cash"),
        ),
        _check(
            "remittance = principal + interest - servicing fee - guarantee fee",
            t.get("principal_collected", 0) + t.get("interest", 0) - t.get("servicing_fee", 0) - t.get("gfee", 0),
            rem["remittance"],
        ),
    ]
    if begin is not None:
        checks.append(
            _check(
                "UPB roll-forward: begin - principal + capitalized = end",
                begin - t.get("principal_collected", 0) + t.get("capitalized", 0),
                end,
            )
        )
    return {
        "pool_id": pool_id,
        "month": month,
        "upb_begin": begin,
        "upb_end": end,
        "checks": checks,
        "ties_out": all(c["ok"] for c in checks),
    }


def delinquency_report(sv: Servicer, as_of) -> dict:
    out = {}
    for pid in sv.pools:
        loans = [lo for lo in sv.loans.values() if lo.pool_id == pid]
        bc = bucket_counts(loans, as_of)
        active = sum(bc["counts"].values())
        total_upb = sum(bc["upb"].values())
        out[pid] = {
            "active_loans": active,
            "upb": total_upb,
            "counts": bc["counts"],
            "upb_by_bucket": bc["upb"],
            "delinquent_bp": (
                (10_000 * sum(bc["counts"][b] for b in BUCKETS[1:]) + active // 2) // active if active else 0
            ),
            "forbearance": sum(1 for lo in loans if lo.status == "active" and lo.state == "forbearance"),
        }
    return out
