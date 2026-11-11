from dataclasses import replace
from datetime import date

import pytest
from conftest import ledger_ties, make_book

from loanservicer.amortization import ArmTerms, arm_reset, is_reset_installment, level_payment, modify, round_to_eighth
from loanservicer.servicing import ServicingError

T56 = ArmTerms(
    margin_mp=2750,
    initial_months=60,
    reset_months=6,
    initial_cap_mp=2000,
    periodic_cap_mp=1000,
    lifetime_cap_mp=5000,
    floor_mp=2750,
)


def test_round_to_eighth():
    assert round_to_eighth(6310) == 6250
    assert round_to_eighth(6313) == 6375  # 6.3125 is a half: rounds up
    assert round_to_eighth(6000) == 6000


def test_reset_uncapped():
    r = arm_reset(T56, index_mp=3400, current_mp=5500, start_mp=5500, first_reset=True)
    assert r.fully_indexed_mp == 6125 and r.new_rate_mp == 6125 and r.limited_by == "none"


def test_initial_cap_limits_first_increase():
    r = arm_reset(T56, index_mp=5300, current_mp=3000, start_mp=3000, first_reset=True)
    assert r.fully_indexed_mp == 8000
    assert r.new_rate_mp == 5000 and r.limited_by == "initial_cap"


def test_periodic_cap_limits_later_increase():
    r = arm_reset(T56, index_mp=5300, current_mp=5000, start_mp=3000, first_reset=False)
    assert r.new_rate_mp == 6000 and r.limited_by == "periodic_cap"


def test_periodic_cap_limits_decrease():
    r = arm_reset(T56, index_mp=500, current_mp=7000, start_mp=5000, first_reset=False)
    assert r.new_rate_mp == 6000 and r.limited_by == "periodic_cap"


def test_lifetime_cap():
    r = arm_reset(T56, index_mp=6000, current_mp=7500, start_mp=3000, first_reset=False)
    assert r.new_rate_mp == 8000 and r.limited_by == "lifetime_cap"


def test_floor():
    terms = replace(T56, floor_mp=3250)
    r = arm_reset(terms, index_mp=0, current_mp=3500, start_mp=3500, first_reset=False)
    assert r.fully_indexed_mp == 2750
    assert r.new_rate_mp == 3250 and r.limited_by == "floor"


def test_reset_installments():
    assert not is_reset_installment(T56, 60)
    assert is_reset_installment(T56, 61)
    assert not is_reset_installment(T56, 62)
    assert is_reset_installment(T56, 67)


def test_servicer_reset_reamortizes_over_remaining_term():
    sv, lo = make_book(
        arm=T56,
        first_due=date(2020, 1, 1),
        installments_paid=60,
        remaining_term=300,
        rate_mp=3000,
        orig_rate_mp=3000,
        principal=18_000_000,
        pi_payment=level_payment(20_000_000, 3000, 360),
    )
    sv.index = {k: 4000 for k in range(2000 * 12, 2040 * 12)}
    ev = sv.maybe_reset_arm(lo, date(2025, 1, 1))
    assert ev["new_rate_mp"] == 5000 and ev["limited_by"] == "initial_cap"
    assert lo.pi_payment == level_payment(18_000_000, 5000, 300)
    assert sv.maybe_reset_arm(lo, date(2025, 2, 1)) is None


def test_recast_lowers_payment_keeps_maturity():
    sv, lo = make_book()
    sv.apply_payment("L1", lo.monthly_payment() + 5_000_000, date(2025, 1, 3))
    assert lo.principal == 20_000_000 - 19_910 - 5_000_000
    ev = sv.recast(lo, date(2025, 1, 10))
    assert ev["new_payment"] == level_payment(lo.principal, 6000, 359) < ev["old_payment"]
    assert lo.remaining_term == 359


def test_recast_refused_during_interest_only():
    sv, lo = make_book(io_months=120)
    with pytest.raises(ServicingError):
        sv.recast(lo, date(2025, 1, 10))


def test_modify_math():
    m = modify(20_000_000, 119_910, 300_000, 4000, 480)
    assert m.new_balance == 20_300_000
    assert m.new_payment == level_payment(20_300_000, 4000, 480)


def test_servicer_modification_capitalizes_arrears_and_waives_fees():
    sv, lo = make_book()
    sv.disburse(lo, "property tax", 300_000, date(2025, 1, 20))  # escrow goes to -1,800.00
    sv.assess_late_fees(lo, date(2025, 3, 20))
    assert lo.late_fees > 0
    ev = sv.modify_loan(lo, date(2025, 4, 1), 4500, 480)
    # 3 installments behind (Jan, Feb, Mar) of $1,000.00 interest, plus the $1,800.00 escrow advance
    assert ev["capitalized"] == 3 * 100_000 + 180_000
    assert lo.principal == 20_480_000 and lo.escrow_balance == 0 and lo.late_fees == 0
    assert lo.next_due == date(2025, 4, 1) and lo.remaining_term == 480 and lo.rate_mp == 4500
    assert lo.pi_payment == level_payment(20_480_000, 4500, 480)
    assert ledger_ties(sv, lo)
