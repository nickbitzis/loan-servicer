"""Exact amortization, ARM rate resets with caps, recast and modification.

Rounding (documented and tested):
- The level P&I payment is B * r / (1 - (1 + r)^-n) computed exactly with
  `Fraction` (r = rate_mp / 1,200,000), then rounded half up to the cent.
- Each month's interest is balance * r rounded half up to the cent.
- Principal is payment minus interest. The final installment pays the
  remaining balance exactly, so it can differ from the level payment by a few
  cents. Principal over the full schedule always sums to the starting balance.
- During an interest-only period the payment is the month's interest and
  principal is zero. At the end of the period the loan amortizes over the
  remaining term.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .money import MONTHLY_DIVISOR, add_months, ceil_div, monthly_interest, round_fraction

EIGHTH = 125  # one eighth of a percent in mp


def level_payment(balance: int, rate_mp: int, months: int) -> int:
    """Level monthly principal and interest payment, in cents."""
    if months <= 0:
        raise ValueError("months must be positive")
    if balance <= 0:
        return 0
    if rate_mp == 0:
        return ceil_div(balance, months)
    r = Fraction(rate_mp, MONTHLY_DIVISOR)
    growth = (1 + r) ** months
    return round_fraction(balance * r * growth / (growth - 1))


@dataclass(frozen=True, slots=True)
class Row:
    n: int
    due: date
    rate_mp: int
    payment: int
    interest: int
    principal: int
    balance: int

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "due": self.due.isoformat(),
            "rate_mp": self.rate_mp,
            "payment": self.payment,
            "interest": self.interest,
            "principal": self.principal,
            "balance": self.balance,
        }


def schedule(
    balance: int,
    rate_mp: int,
    months: int,
    first_due: date,
    io_months: int = 0,
    payment: int | None = None,
    start_n: int = 1,
) -> list[Row]:
    """Full schedule from `balance` over `months` installments at a fixed rate."""
    rows: list[Row] = []
    amort_months = months - io_months
    pmt = payment if payment is not None else level_payment(balance, rate_mp, max(amort_months, 1))
    bal = balance
    for i in range(months):
        interest = monthly_interest(bal, rate_mp)
        if i < io_months:
            principal = 0
            pay = interest
        elif i == months - 1:
            principal = bal
            pay = interest + principal
        else:
            principal = min(max(pmt - interest, 0), bal)
            pay = interest + principal
        bal -= principal
        rows.append(Row(start_n + i, add_months(first_due, i), rate_mp, pay, interest, principal, bal))
        if bal == 0:
            break
    return rows


def round_to_eighth(rate_mp: int) -> int:
    """Nearest 1/8 percent, halves up."""
    return ((rate_mp + EIGHTH // 2) // EIGHTH) * EIGHTH


@dataclass(frozen=True, slots=True)
class ArmTerms:
    margin_mp: int
    initial_months: int  # fixed period before the first reset
    reset_months: int  # months between later resets
    initial_cap_mp: int
    periodic_cap_mp: int
    lifetime_cap_mp: int  # over the start rate
    floor_mp: int  # usually the margin

    def to_dict(self) -> dict:
        return {
            "margin_mp": self.margin_mp,
            "initial_months": self.initial_months,
            "reset_months": self.reset_months,
            "initial_cap_mp": self.initial_cap_mp,
            "periodic_cap_mp": self.periodic_cap_mp,
            "lifetime_cap_mp": self.lifetime_cap_mp,
            "floor_mp": self.floor_mp,
        }


@dataclass(frozen=True, slots=True)
class ResetResult:
    index_mp: int
    fully_indexed_mp: int
    old_rate_mp: int
    new_rate_mp: int
    limited_by: str  # "none", "initial_cap", "periodic_cap", "lifetime_cap", "floor"


def arm_reset(terms: ArmTerms, index_mp: int, current_mp: int, start_mp: int, first_reset: bool) -> ResetResult:
    """New note rate at a reset: index + margin, rounded to 1/8, then capped.

    Order: the per-adjustment cap (initial or periodic) first, then the lifetime
    ceiling and the floor.
    """
    fully = round_to_eighth(index_mp + terms.margin_mp)
    cap = terms.initial_cap_mp if first_reset else terms.periodic_cap_mp
    new = fully
    limited = "none"
    if new > current_mp + cap:
        new, limited = current_mp + cap, "initial_cap" if first_reset else "periodic_cap"
    elif new < current_mp - cap:
        new, limited = current_mp - cap, "initial_cap" if first_reset else "periodic_cap"
    ceiling = start_mp + terms.lifetime_cap_mp
    if new > ceiling:
        new, limited = ceiling, "lifetime_cap"
    if new < terms.floor_mp:
        new, limited = terms.floor_mp, "floor"
    return ResetResult(index_mp, fully, current_mp, new, limited)


def is_reset_installment(terms: ArmTerms, installment_n: int) -> bool:
    """True when installment number `installment_n` (1-based) is the first at a new rate."""
    k = installment_n - 1
    if k < terms.initial_months:
        return False
    return (k - terms.initial_months) % terms.reset_months == 0


def recast_payment(balance: int, rate_mp: int, remaining_months: int) -> int:
    """New P&I after a large curtailment: same rate, same maturity, lower payment."""
    return level_payment(balance, rate_mp, remaining_months)


@dataclass(frozen=True, slots=True)
class Modification:
    new_rate_mp: int
    new_term_months: int
    capitalized: int  # arrears added to principal
    old_balance: int
    new_balance: int
    old_payment: int
    new_payment: int

    def to_dict(self) -> dict:
        return {
            "new_rate_mp": self.new_rate_mp,
            "new_term_months": self.new_term_months,
            "capitalized": self.capitalized,
            "old_balance": self.old_balance,
            "new_balance": self.new_balance,
            "old_payment": self.old_payment,
            "new_payment": self.new_payment,
        }


def modify(balance: int, old_payment: int, capitalized: int, new_rate_mp: int, new_term_months: int) -> Modification:
    new_balance = balance + capitalized
    return Modification(
        new_rate_mp=new_rate_mp,
        new_term_months=new_term_months,
        capitalized=capitalized,
        old_balance=balance,
        new_balance=new_balance,
        old_payment=old_payment,
        new_payment=level_payment(new_balance, new_rate_mp, new_term_months),
    )
