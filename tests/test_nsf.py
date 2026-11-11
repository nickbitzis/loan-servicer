from datetime import date

import pytest
from conftest import ledger_ties, make_book

from loanservicer.models import InvestorRules
from loanservicer.servicing import ServicingError

INSTALLMENT = 149_910


def state(lo):
    return (
        lo.principal,
        lo.escrow_balance,
        lo.suspense,
        lo.unapplied,
        lo.next_due,
        lo.remaining_term,
        lo.shortage_outstanding,
    )


def test_reversal_restores_state_and_adds_nsf_fee():
    sv, lo = make_book()
    before = state(lo)
    t = sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 2), "autopay")
    res = sv.reverse_payment("L1", t.txn_id, date(2025, 1, 6))
    assert state(lo) == before
    assert lo.late_fees == 2_500 and lo.nsf_count == 1
    assert t.reversed_by == res["reversal"]
    assert ledger_ties(sv, lo)


def test_lifo_unwind_and_reapply_matches_a_book_that_never_saw_the_bad_payment():
    sv, lo = make_book()
    bad = sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 2), "autopay")
    sv.apply_payment("L1", 80_000, date(2025, 1, 4))  # applied on top of the bad one: goes toward February
    res = sv.reverse_payment("L1", bad.txn_id, date(2025, 1, 6), fee=False)
    assert len(res["unwound"]) == 1 and len(res["reapplied"]) == 1

    sv2, lo2 = make_book()
    sv2.apply_payment("L1", 80_000, date(2025, 1, 6))
    assert state(lo) == state(lo2)
    assert lo.suspense == 80_000 and lo.next_due == date(2025, 1, 1)
    assert ledger_ties(sv, lo)


def test_reversal_under_apply_policy_restores_partial_installment():
    sv, lo = make_book(InvestorRules(partial_policy="apply"))
    sv.apply_payment("L1", 50_000, date(2025, 1, 2))
    t = sv.apply_payment("L1", 50_000, date(2025, 1, 3))
    sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 4))
    sv.reverse_payment("L1", t.txn_id, date(2025, 1, 8), fee=False)

    sv2, lo2 = make_book(InvestorRules(partial_policy="apply"))
    sv2.apply_payment("L1", 50_000, date(2025, 1, 2))
    sv2.apply_payment("L1", INSTALLMENT, date(2025, 1, 8))
    assert state(lo) == state(lo2)
    assert lo.inst_state == lo2.inst_state
    assert ledger_ties(sv, lo)


def test_cannot_reverse_twice():
    sv, lo = make_book()
    t = sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 2))
    sv.reverse_payment("L1", t.txn_id, date(2025, 1, 6))
    with pytest.raises(ServicingError):
        sv.reverse_payment("L1", t.txn_id, date(2025, 1, 7))


def test_reversal_window():
    sv, lo = make_book()
    t = sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 2))
    with pytest.raises(ServicingError):
        sv.reverse_payment("L1", t.txn_id, date(2025, 3, 15))


def test_cannot_unwind_across_recast():
    sv, lo = make_book()
    t = sv.apply_payment("L1", INSTALLMENT + 2_000_000, date(2025, 1, 2))
    sv.recast(lo, date(2025, 1, 4))
    with pytest.raises(ServicingError):
        sv.reverse_payment("L1", t.txn_id, date(2025, 1, 6))


def test_reversed_installment_becomes_late():
    sv, lo = make_book()
    t = sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 2))
    sv.reverse_payment("L1", t.txn_id, date(2025, 1, 6), fee=False)
    assert sv.assess_late_fees(lo, date(2025, 1, 17)) == 5_996  # 5% of $1,199.10
