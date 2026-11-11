"""Days past due, delinquency buckets, roll rates and the collections queue.

Buckets use the months-behind convention: an installment counts as missed once
it is still unpaid at the end of the day before the next due date. A loan due
on Jan 1 that has not paid is "30" on Jan 31 and "60" on Feb 28.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import Loan
from .money import add_months, div_round

BUCKETS = ("current", "30", "60", "90", "120+")
STATES = BUCKETS + ("paid_off",)


def days_past_due(loan: Loan, as_of: date) -> int:
    if loan.status != "active" or loan.next_due > as_of:
        return 0
    return (as_of - loan.next_due).days


def months_behind(loan: Loan, as_of: date) -> int:
    if loan.status != "active":
        return 0
    k = 0
    d = loan.next_due
    nxt = as_of + timedelta(days=1)
    while add_months(d, 1) <= nxt and k < 600:
        k += 1
        d = add_months(d, 1)
    return k


def bucket(loan: Loan, as_of: date) -> str:
    if loan.status != "active":
        return "paid_off"
    return BUCKETS[min(months_behind(loan, as_of), 4)]


def roll_rates(prev: dict[str, str], cur: dict[str, str]) -> dict:
    """Transition counts and rates from one month-end to the next.

    Rows are last month's bucket; columns are this month's bucket or paid_off.
    """
    counts = {a: dict.fromkeys(STATES, 0) for a in BUCKETS}
    for loan_id, a in prev.items():
        if a == "paid_off":
            continue
        b = cur.get(loan_id, "paid_off")
        counts[a][b] += 1
    rates = {}
    for a, row in counts.items():
        n = sum(row.values())
        rates[a] = {b: (div_round(v * 10_000, n) if n else 0) for b, v in row.items()}
    return {"counts": counts, "rates_bp": rates}


def roll_forward_bp(rr: dict, a: str) -> int:
    """Share of loans in bucket `a` that rolled to a worse bucket, in basis points."""
    row = rr["counts"][a]
    n = sum(row.values())
    if not n:
        return 0
    i = BUCKETS.index(a)
    worse = sum(row[b] for b in BUCKETS[i + 1 :])
    return div_round(worse * 10_000, n)


def bucket_counts(loans, as_of: date) -> dict:
    counts = dict.fromkeys(BUCKETS, 0)
    upb = dict.fromkeys(BUCKETS, 0)
    for lo in loans:
        if lo.status != "active":
            continue
        b = bucket(lo, as_of)
        counts[b] += 1
        upb[b] += lo.principal
    return {"counts": counts, "upb": upb}


def risk_score(loan: Loan, as_of: date) -> tuple[int, list[str]]:
    dpd = days_past_due(loan, as_of)
    score = min(dpd, 150)
    factors = [f"{dpd} days past due"]
    if loan.borrower.fico < 640:
        score += 25
        factors.append("credit score under 640")
    elif loan.borrower.fico < 680:
        score += 15
        factors.append("credit score under 680")
    if loan.nsf_count:
        score += 10 * min(loan.nsf_count, 3)
        factors.append(f"{loan.nsf_count} returned payments")
    if loan.plan and loan.plan.get("status") == "broken":
        score += 20
        factors.append("broken payment plan")
    if loan.principal > 40_000_000:
        score += 10
        factors.append("balance over $400k")
    if months_behind(loan, as_of) == 0 and dpd > 0:
        score += 5
        factors.append("early stage, best chance to cure")
    return score, factors


def next_action(loan: Loan, as_of: date) -> tuple[str, str]:
    """Contact rules, checked in order. Returns (action, reason)."""
    b = loan.borrower
    if b.bankruptcy:
        return "no_contact", "bankruptcy: collection contact stopped"
    if loan.state == "forbearance":
        return "loss_mit_team", "in forbearance: no collection calls"
    if loan.state == "loss_mit":
        return "loss_mit_team", "in loss mitigation review"
    if b.cease_contact:
        return "letter_only", "borrower asked us to stop calling"
    week_ago = as_of - timedelta(days=7)
    recent = [d for d in loan.contact_log if d > week_ago]
    if len(recent) >= 7:
        nxt = (min(recent) + timedelta(days=7)).isoformat()
        return "call_blocked", f"7 call attempts in the last 7 days; next call on or after {nxt}"
    if loan.plan and loan.plan.get("status") == "active":
        return "monitor_plan", "on a payment plan"
    mb = months_behind(loan, as_of)
    channel = "email" if b.contact_pref == "email" else "call"
    if mb >= 3:
        return "refer_loss_mit", "90 or more days behind: send to loss mitigation"
    if mb == 2:
        return f"{channel}_offer_plan", "60 days behind: offer a repayment plan"
    if mb == 1:
        return channel, "30 days behind"
    return f"{channel}_reminder", "past the grace period"


def collections_queue(loans, as_of: date, grace_days: int = 15) -> list[dict]:
    out = []
    for lo in loans:
        if lo.status != "active":
            continue
        dpd = days_past_due(lo, as_of)
        if dpd <= grace_days:
            continue
        score, factors = risk_score(lo, as_of)
        action, reason = next_action(lo, as_of)
        out.append(
            {
                "loan_id": lo.loan_id,
                "borrower": lo.borrower.name,
                "state_code": lo.borrower.state,
                "pool_id": lo.pool_id,
                "dpd": dpd,
                "bucket": bucket(lo, as_of),
                "amount_due": lo.installments_total(len(lo.unpaid_dues(as_of))) + lo.late_fees - lo.suspense,
                "principal": lo.principal,
                "score": score,
                "factors": factors,
                "action": action,
                "reason": reason,
                "servicing_state": lo.state,
            }
        )
    out.sort(key=lambda r: (-r["score"], r["loan_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
