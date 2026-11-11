import random
from datetime import date

import pytest
from conftest import ledger_ties, make_book

from loanservicer.models import InvestorRules
from loanservicer.servicing import ServicingError

D = date(2025, 1, 5)
INSTALLMENT = 100_000 + 19_910 + 30_000  # interest + principal + escrow on the first installment


def test_exact_installment_applies_interest_principal_escrow():
    sv, lo = make_book()
    assert lo.monthly_payment() == INSTALLMENT
    t = sv.apply_payment("L1", INSTALLMENT, D)
    assert t.splits == {"interest": 100_000, "principal": 19_910, "escrow": 30_000, "servicing_fee": 4_167}
    assert t.installments == ["2025-01-01"]
    assert lo.next_due == date(2025, 2, 1) and lo.principal == 20_000_000 - 19_910
    assert lo.escrow_balance == 150_000
    assert ledger_ties(sv, lo)


ORDERS = [
    ("late_fees", "escrow_shortage", "interest", "principal", "escrow"),
    ("interest", "principal", "escrow", "escrow_shortage", "late_fees"),
    ("escrow_shortage", "interest", "principal", "escrow", "late_fees"),
    ("escrow", "interest", "principal", "late_fees", "escrow_shortage"),
]


@pytest.mark.parametrize("order", ORDERS)
def test_order_decides_whether_fees_block_the_installment(order):
    sv, lo = make_book(InvestorRules(order=order, partial_policy="suspense"))
    lo.late_fees = 5_000
    t = sv.apply_payment("L1", INSTALLMENT, D)
    fees_first = order.index("late_fees") < min(order.index(c) for c in ("interest", "principal", "escrow"))
    if fees_first:
        # fees come first, so what is left cannot cover the installment: everything is held
        assert t.installments == [] and lo.suspense == INSTALLMENT and lo.late_fees == 5_000
    else:
        assert t.installments == ["2025-01-01"] and lo.late_fees == 5_000 and lo.suspense == 0
    assert ledger_ties(sv, lo)


@pytest.mark.parametrize("order", ORDERS)
def test_apply_policy_partial_follows_order(order):
    sv, lo = make_book(InvestorRules(order=order, partial_policy="apply"))
    t = sv.apply_payment("L1", 40_000, D)
    first = next(c for c in order if c not in ("late_fees", "escrow_shortage"))
    want = {"interest": 100_000, "principal": 19_910, "escrow": 30_000}[first]
    assert t.splits[first] == min(40_000, want)
    assert lo.next_due == date(2025, 1, 1)  # installment not complete
    sv.apply_payment("L1", INSTALLMENT - 40_000, D)
    assert lo.next_due == date(2025, 2, 1)
    assert lo.principal == 20_000_000 - 19_910
    assert ledger_ties(sv, lo)


def test_fees_taken_after_installment_when_fees_last():
    sv, lo = make_book()
    lo.late_fees = 5_000
    t = sv.apply_payment("L1", INSTALLMENT + 5_000, D)
    assert t.splits["late_fees"] == 5_000 and lo.late_fees == 0 and t.installments == ["2025-01-01"]


def test_partial_held_in_suspense_then_released():
    sv, lo = make_book()
    t1 = sv.apply_payment("L1", 100_000, D)
    assert t1.splits == {"suspense": 100_000} and lo.suspense == 100_000 and lo.next_due == date(2025, 1, 1)
    t2 = sv.apply_payment("L1", INSTALLMENT - 100_000, date(2025, 1, 12))
    assert t2.splits["suspense"] == -100_000 and lo.suspense == 0
    assert t2.installments == ["2025-01-01"]
    assert ledger_ties(sv, lo)


def test_two_installments_in_one_payment():
    sv, lo = make_book()
    total = lo.installments_total(2)
    t = sv.apply_payment("L1", total, date(2025, 2, 20))
    assert t.installments == ["2025-01-01", "2025-02-01"] and lo.suspense == 0 and lo.unapplied == 0


def test_excess_goes_to_curtailment():
    sv, lo = make_book()
    t = sv.apply_payment("L1", INSTALLMENT + 50_000, D)
    assert t.splits["curtailment"] == 50_000
    assert lo.principal == 20_000_000 - 19_910 - 50_000


def test_extra_smaller_than_next_installment_is_curtailment_not_suspense():
    sv, lo = make_book()
    t = sv.apply_payment("L1", INSTALLMENT + 20_000, date(2025, 1, 2))
    assert t.splits.get("suspense", 0) == 0 and t.splits["curtailment"] == 20_000


def test_paying_next_month_early_when_investor_allows_pay_ahead():
    sv, lo = make_book(InvestorRules(pay_ahead=True))
    sv.apply_payment("L1", INSTALLMENT, D)
    t = sv.apply_payment("L1", lo.monthly_payment(), date(2025, 1, 28))
    assert t.installments == ["2025-02-01"] and lo.next_due == date(2025, 3, 1)


def test_early_payment_without_pay_ahead_is_a_curtailment():
    sv, lo = make_book()
    sv.apply_payment("L1", INSTALLMENT, D)
    t = sv.apply_payment("L1", 150_000, date(2025, 1, 28))
    assert t.installments == [] and t.splits == {"curtailment": 150_000}


def test_excess_goes_to_unapplied_then_used_next_time():
    sv, lo = make_book(InvestorRules(excess="unapplied"))
    sv.apply_payment("L1", INSTALLMENT + 50_000, D)
    assert lo.unapplied == 50_000
    t = sv.apply_payment("L1", lo.monthly_payment() - 50_000, date(2025, 2, 3))
    assert t.splits["unapplied"] == -50_000 and t.installments == ["2025-02-01"] and lo.unapplied == 0
    assert ledger_ties(sv, lo)


def test_escrow_shortage_collected_with_installment():
    sv, lo = make_book()
    lo.shortage_outstanding, lo.shortage_monthly = 24_000, 2_000
    assert lo.monthly_payment() == INSTALLMENT + 2_000
    t = sv.apply_payment("L1", INSTALLMENT + 2_000, D)
    assert t.splits["escrow_shortage"] == 2_000 and lo.shortage_outstanding == 22_000


def test_servicing_and_guarantee_fee_strip():
    sv, lo = make_book(InvestorRules(servicing_fee_mp=250, gfee_mp=450))
    t = sv.apply_payment("L1", INSTALLMENT, D)
    # interest $1,000.00 at 6%: 0.25% / 6% and 0.45% / 6% of it
    assert t.splits["servicing_fee"] == 4_167 and t.splits["gfee"] == 7_500


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("policy", ["suspense", "apply"])
def test_random_payments_always_split_to_the_cent(seed, policy):
    rng = random.Random(seed)
    sv, lo = make_book(InvestorRules(partial_policy=policy, order=rng.choice(ORDERS)))
    lo.late_fees = 2_500
    for i in range(10):
        amt = rng.randint(1_000, 400_000)
        t = sv.apply_payment("L1", amt, date(2025, 1 + i, rng.randint(1, 28)))
        s = t.splits
        parts = (
            "late_fees",
            "escrow_shortage",
            "interest",
            "principal",
            "escrow",
            "curtailment",
            "suspense",
            "unapplied",
        )
        assert sum(s.get(k, 0) for k in parts) == amt
    assert ledger_ties(sv, lo)
    assert lo.principal > 0 and lo.suspense >= 0


def test_rejects_bad_amounts_and_paid_off_loans():
    sv, lo = make_book()
    with pytest.raises(ServicingError):
        sv.apply_payment("L1", 0, D)
    with pytest.raises(TypeError):
        sv.apply_payment("L1", 1000.0, D)
    sv.process_payoff(lo, date(2025, 1, 20))
    with pytest.raises(ServicingError):
        sv.apply_payment("L1", 10_000, D)


def test_bad_order_rejected():
    with pytest.raises(ValueError):
        InvestorRules(order=("interest", "principal"))
