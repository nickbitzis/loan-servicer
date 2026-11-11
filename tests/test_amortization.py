from datetime import date

import pytest

from loanservicer.amortization import level_payment, schedule
from loanservicer.money import add_months, div_round, monthly_interest, require_cents

# Level payments that match any standard mortgage table.
KNOWN = [
    (20_000_000, 6000, 360, 119_910),  # $200,000 at 6% for 30 years: $1,199.10
    (10_000_000, 5000, 360, 53_682),  # $100,000 at 5%: $536.82
    (30_000_000, 7000, 360, 199_591),  # $300,000 at 7%: $1,995.91
    (15_000_000, 4500, 180, 114_749),  # $150,000 at 4.5% for 15 years: $1,147.49
    (25_000_000, 3250, 360, 108_802),  # $250,000 at 3.25%: $1,088.02
    (40_000_000, 6875, 240, 307_125),  # $400,000 at 6.875% for 20 years: $3,071.25
]


@pytest.mark.parametrize("balance,rate,months,expected", KNOWN)
def test_level_payment_known_values(balance, rate, months, expected):
    assert level_payment(balance, rate, months) == expected


@pytest.mark.parametrize("balance,rate,months,expected", KNOWN)
def test_schedule_pays_to_zero_and_principal_sums(balance, rate, months, expected):
    rows = schedule(balance, rate, months, date(2025, 1, 1))
    assert len(rows) == months
    assert rows[-1].balance == 0
    assert sum(r.principal for r in rows) == balance
    assert all(r.payment == r.interest + r.principal for r in rows)
    assert all(r.payment == expected for r in rows[:-1])
    assert abs(rows[-1].payment - expected) < 500  # final installment absorbs rounding


def test_first_row_and_total_interest_200k_6pct():
    rows = schedule(20_000_000, 6000, 360, date(2025, 1, 1))
    assert rows[0].interest == 100_000 and rows[0].principal == 19_910
    assert rows[1].interest == monthly_interest(20_000_000 - 19_910, 6000) == 99_900
    assert sum(r.interest for r in rows) == 23_167_704
    assert rows[-1].payment == 120_014
    assert rows[11].due == date(2025, 12, 1)


def test_zero_rate_payment_rounds_up():
    assert level_payment(10_000_000, 0, 360) == 27_778
    rows = schedule(10_000_000, 0, 360, date(2025, 1, 1))
    assert rows[-1].balance == 0 and sum(r.principal for r in rows) == 10_000_000


def test_interest_only_period_then_amortizes():
    rows = schedule(30_000_000, 6000, 360, date(2025, 1, 1), io_months=120)
    assert all(r.principal == 0 and r.payment == 150_000 for r in rows[:120])
    amort = level_payment(30_000_000, 6000, 240)
    assert rows[120].payment == amort
    assert rows[-1].balance == 0


def test_div_round_half_up():
    assert div_round(5, 10) == 1
    assert div_round(4, 10) == 0
    assert div_round(15, 10) == 2
    assert div_round(-5, 10) == -1
    assert div_round(-4, 10) == 0


def test_monthly_interest_rounds_half_up_at_half_cent():
    # 100 cents at 6% for a month is exactly 0.5 cents
    assert monthly_interest(100, 6000) == 1
    assert monthly_interest(99, 6000) == 0


def test_floats_rejected():
    with pytest.raises(TypeError):
        require_cents(10.5)
    with pytest.raises(TypeError):
        require_cents(True)


def test_add_months_clamps_month_end():
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2025, 11, 1), 3) == date(2026, 2, 1)
