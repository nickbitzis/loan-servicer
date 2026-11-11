"""Integer money and rate helpers.

Amounts are int cents. Rates are int thousandths of a percent ("mp"), so
6.125% is 6125 and one eighth of a point is 125. Nothing here uses float.

Rounding rule used everywhere: round half up (away from zero for negatives),
done with integer arithmetic in `div_round`.
"""

from __future__ import annotations

import calendar
from datetime import date
from fractions import Fraction

MP_PER_PERCENT = 1000
# monthly rate = rate_mp / 1000 / 100 / 12
MONTHLY_DIVISOR = 1_200_000


def require_cents(value: object, field: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be int cents, got {type(value).__name__}")
    return value


def div_round(n: int, d: int) -> int:
    """n / d rounded half up (half away from zero), integers only."""
    if d <= 0:
        raise ValueError("divisor must be positive")
    if n >= 0:
        return (2 * n + d) // (2 * d)
    return -((-2 * n + d) // (2 * d))


def round_fraction(x: Fraction) -> int:
    return div_round(x.numerator, x.denominator)


def ceil_div(n: int, d: int) -> int:
    return -((-n) // d)


def monthly_interest(balance: int, rate_mp: int) -> int:
    """One month of interest on `balance` at annual `rate_mp`, rounded half up to the cent."""
    return div_round(balance * rate_mp, MONTHLY_DIVISOR)


def fmt(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def fmt_rate(rate_mp: int) -> str:
    return f"{rate_mp // 1000}.{rate_mp % 1000:03d}%"


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    if d.day <= 28:
        return date(y, m, d.day)
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def days_between(a: date, b: date) -> int:
    return (b - a).days


__all__ = [
    "add_months",
    "ceil_div",
    "days_between",
    "div_round",
    "fmt",
    "fmt_rate",
    "month_end",
    "month_index",
    "monthly_interest",
    "require_cents",
    "round_fraction",
    "ym",
]
