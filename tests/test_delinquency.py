from datetime import date

from conftest import make_book

from loanservicer.delinquency import (
    bucket,
    collections_queue,
    days_past_due,
    months_behind,
    roll_forward_bp,
    roll_rates,
)
from loanservicer.models import InvestorRules

INSTALLMENT = 149_910


def test_no_late_fee_inside_grace_period():
    sv, lo = make_book()
    assert sv.assess_late_fees(lo, date(2025, 1, 16)) == 0  # 15 days after the due date
    assert lo.late_fees == 0


def test_late_fee_on_day_after_grace():
    sv, lo = make_book()
    assert sv.assess_late_fees(lo, date(2025, 1, 17)) == 5_996  # 5% of $1,199.10 P&I
    assert lo.late_fees == 5_996


def test_payment_on_last_grace_day_avoids_fee():
    sv, lo = make_book()
    sv.apply_payment("L1", INSTALLMENT, date(2025, 1, 16))
    assert sv.assess_late_fees(lo, date(2025, 1, 17)) == 0


def test_late_fee_once_per_installment_and_capped():
    sv, lo = make_book(InvestorRules(late_fee_bp=1000, late_fee_cap=10_000))
    assert sv.assess_late_fees(lo, date(2025, 1, 17)) == 10_000
    assert sv.assess_late_fees(lo, date(2025, 1, 25)) == 0
    assert sv.assess_late_fees(lo, date(2025, 2, 17)) == 10_000  # February's installment


def test_shorter_grace_period():
    sv, lo = make_book(InvestorRules(grace_days=10))
    assert sv.assess_late_fees(lo, date(2025, 1, 11)) == 0
    assert sv.assess_late_fees(lo, date(2025, 1, 12)) > 0


def test_no_late_fees_in_forbearance():
    sv, lo = make_book()
    lo.state = "forbearance"
    assert sv.assess_late_fees(lo, date(2025, 3, 17)) == 0


def test_months_behind_convention():
    _, lo = make_book()
    assert months_behind(lo, date(2025, 1, 30)) == 0
    assert months_behind(lo, date(2025, 1, 31)) == 1
    assert months_behind(lo, date(2025, 2, 28)) == 2
    assert days_past_due(lo, date(2025, 2, 28)) == 58


def test_bucket_transitions_as_months_pass_without_payment():
    _, lo = make_book()
    ends = [date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 31), date(2025, 4, 30), date(2025, 7, 31)]
    assert [bucket(lo, d) for d in ends] == ["30", "60", "90", "120+", "120+"]


def test_cure_back_to_current():
    sv, lo = make_book()
    assert bucket(lo, date(2025, 1, 31)) == "30"
    sv.apply_payment("L1", lo.installments_total(2), date(2025, 2, 10))
    assert bucket(lo, date(2025, 2, 28)) == "current"


def test_roll_rates_counts_and_rates():
    prev = {"a": "current", "b": "current", "c": "30", "d": "30", "e": "60", "f": "paid_off"}
    cur = {"a": "current", "b": "30", "c": "60", "d": "current", "f": "paid_off"}  # e paid off
    rr = roll_rates(prev, cur)
    assert rr["counts"]["current"]["30"] == 1 and rr["counts"]["current"]["current"] == 1
    assert rr["counts"]["30"]["60"] == 1 and rr["counts"]["30"]["current"] == 1
    assert rr["counts"]["60"]["paid_off"] == 1
    assert rr["rates_bp"]["current"]["30"] == 5_000
    assert roll_forward_bp(rr, "30") == 5_000
    assert all(sum(row.values()) in (0, 10_000) for row in rr["rates_bp"].values())


def test_collections_queue_rules():
    loans = []
    for i, setup in enumerate(["plain", "bankrupt", "forbearance", "cease", "calls", "lowfico"]):
        _, lo = make_book(loan_id=f"L{i}")
        if setup == "bankrupt":
            lo.borrower.bankruptcy = True
        elif setup == "forbearance":
            lo.state = "forbearance"
        elif setup == "cease":
            lo.borrower.cease_contact = True
        elif setup == "calls":
            lo.contact_log = [date(2025, 2, d) for d in range(4, 11)]
        elif setup == "lowfico":
            lo.borrower.fico = 610
        loans.append(lo)
    q = collections_queue(loans, date(2025, 2, 10))
    by_id = {r["loan_id"]: r for r in q}
    assert by_id["L0"]["action"] == "call" and by_id["L0"]["bucket"] == "30"
    assert by_id["L1"]["action"] == "no_contact"
    assert by_id["L2"]["action"] == "loss_mit_team"
    assert by_id["L3"]["action"] == "letter_only"
    assert by_id["L4"]["action"] == "call_blocked"
    assert q[0]["loan_id"] == "L5"  # low credit score ranks first at equal days past due
    assert [r["rank"] for r in q] == list(range(1, len(q) + 1))


def test_queue_skips_loans_inside_grace():
    _, lo = make_book()
    assert collections_queue([lo], date(2025, 1, 16)) == []
    assert len(collections_queue([lo], date(2025, 1, 17))) == 1


def test_queue_escalates_at_60_and_90():
    _, lo = make_book()
    assert collections_queue([lo], date(2025, 2, 28))[0]["action"] == "call_offer_plan"
    assert collections_queue([lo], date(2025, 3, 31))[0]["action"] == "refer_loss_mit"
