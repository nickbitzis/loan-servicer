"""Annual escrow analysis: projected disbursements, cushion, shortage and surplus.

Illustrative rules modeled on the common shape of an annual analysis, not any
regulator's or agency's exact method:
- Project the next 12 months of tax and insurance disbursements.
- New monthly deposit = annual disbursements / 12, rounded up to the cent.
- Cushion = the investor's cushion months of deposits, capped at 1/6 of the
  annual disbursements.
- Walk the projected balance month by month (deposit, then disbursements) from
  the current balance plus deposits still owed on unpaid installments. The
  lowest point is compared with the cushion.
- Shortage (low point below the cushion) is spread over the investor's spread
  months, or collected as a lump sum. Surplus of at least the refund minimum on
  a current loan is refunded; a smaller surplus stays in the account.
"""

from __future__ import annotations

from datetime import date

from .models import Loan
from .money import add_months, ceil_div, fmt


def projected_disbursements(loan: Loan, start: date, months: int = 12) -> list[dict]:
    out = []
    first_tax = True
    for i in range(months):
        d = add_months(start, i)
        if loan.ins_month == d.month and loan.annual_ins:
            out.append(
                {"date": date(d.year, d.month, 10).isoformat(), "kind": "hazard insurance", "amount": loan.annual_ins}
            )
        if d.month in loan.tax_months and loan.annual_tax:
            half = loan.annual_tax // 2
            amt = half if first_tax else loan.annual_tax - half
            first_tax = not first_tax
            out.append({"date": date(d.year, d.month, 20).isoformat(), "kind": "property tax", "amount": amt})
    return out


def tax_installment(loan: Loan, month: int) -> int:
    """Amount of the property tax installment in `month` (first half rounds down)."""
    half = loan.annual_tax // 2
    return half if month == loan.tax_months[0] else loan.annual_tax - half


def analyze(loan: Loan, effective: date, rules, spread: str | int | None = None) -> dict:
    """Escrow analysis for the 12 months starting at `effective` (a due date)."""
    disb = projected_disbursements(loan, effective)
    annual = sum(x["amount"] for x in disb)
    monthly = ceil_div(annual, 12)
    cushion = min(monthly * rules.cushion_months, annual // 6)
    unpaid_before = [d for d in loan.unpaid_dues(add_months(effective, -1)) if d < effective]
    owed_deposits = len(unpaid_before) * loan.escrow_monthly
    start_balance = loan.escrow_balance + owed_deposits
    bal = start_balance
    rows = []
    low = None
    low_month = None
    for i in range(12):
        d = add_months(effective, i)
        out = sum(x["amount"] for x in disb if x["date"][:7] == d.isoformat()[:7])
        bal += monthly - out
        rows.append({"month": d.isoformat()[:7], "deposit": monthly, "disbursements": out, "balance": bal})
        if low is None or bal < low:
            low, low_month = bal, d.isoformat()[:7]
    low = low or 0
    shortage = max(0, cushion - low)
    deficiency = max(0, -low)
    surplus = max(0, low - cushion)
    spread_months = rules.shortage_spread_months if spread in (None, "") else spread
    current = not unpaid_before
    if shortage:
        if spread_months == "lump":
            action = f"shortage of {fmt(shortage)} due as a lump sum"
            shortage_monthly = shortage
            months = 1
        else:
            months = int(spread_months)
            shortage_monthly = ceil_div(shortage, months)
            action = f"shortage of {fmt(shortage)} spread over {months} months ({fmt(shortage_monthly)} a month)"
    else:
        shortage_monthly = 0
        months = 0
        if surplus >= rules.surplus_refund_min and current:
            action = f"surplus of {fmt(surplus)} refunded"
        elif surplus:
            action = f"surplus of {fmt(surplus)} kept in the account" + ("" if current else " (loan not current)")
        else:
            action = "no shortage or surplus"
    refund = surplus if (not shortage and surplus >= rules.surplus_refund_min and current) else 0
    old_total = loan.pi_payment + loan.escrow_monthly + loan.shortage_monthly
    new_total = loan.pi_payment + monthly + shortage_monthly
    notice = [
        f"Annual escrow account disclosure for loan {loan.loan_id}",
        f"Escrow year starting {effective.isoformat()}. Projected disbursements: {fmt(annual)}.",
        f"Required cushion: {fmt(cushion)}. Projected low point: {fmt(low)} in {low_month}.",
        f"Result: {action}.",
        f"Monthly escrow deposit changes from {fmt(loan.escrow_monthly)} to {fmt(monthly)}.",
        f"Your total monthly payment changes from {fmt(old_total)} to {fmt(new_total)}"
        f" starting with the payment due {effective.isoformat()}.",
    ]
    return {
        "loan_id": loan.loan_id,
        "effective": effective.isoformat(),
        "disbursements": disb,
        "annual_disbursements": annual,
        "new_monthly_deposit": monthly,
        "old_monthly_deposit": loan.escrow_monthly,
        "cushion": cushion,
        "start_balance": start_balance,
        "deposits_owed_on_unpaid_installments": owed_deposits,
        "projection": rows,
        "low_point": low,
        "low_month": low_month,
        "shortage": shortage,
        "deficiency": deficiency,
        "surplus": surplus,
        "refund": refund,
        "shortage_monthly": shortage_monthly,
        "spread_months": months,
        "action": action,
        "old_total_payment": old_total,
        "new_total_payment": new_total,
        "notice": notice,
    }


def apply_analysis(servicer, loan: Loan, result: dict, on: date) -> None:
    loan.escrow_monthly = result["new_monthly_deposit"]
    loan.shortage_outstanding = result["shortage"]
    loan.shortage_monthly = result["shortage_monthly"]
    if loan.inst_state is not None and not any(loan.inst_state["paid"].values()):
        loan.inst_state = None
    if result["refund"]:
        servicer.refund_escrow(loan, result["refund"], on, "escrow surplus refund")
    loan.events.append(
        {
            "on": on.isoformat(),
            "event": "escrow_analysis",
            "shortage": result["shortage"],
            "surplus": result["surplus"],
            "new_monthly_deposit": result["new_monthly_deposit"],
            "action": result["action"],
        }
    )
