from datetime import date

from conftest import ledger_ties, make_book

from loanservicer.escrow import analyze, apply_analysis
from loanservicer.models import InvestorRules
from loanservicer.money import ceil_div


def current_book(**kw):
    sv, lo = make_book(**kw)
    return sv, lo


def test_cushion_capped_at_one_sixth():
    sv, lo = make_book()
    r = analyze(lo, date(2025, 1, 1), InvestorRules(cushion_months=4))
    assert r["annual_disbursements"] == 360_000 and r["cushion"] == 60_000


def test_shortage_spread_over_twelve_months():
    sv, lo = make_book(escrow_balance=0)
    lo.annual_tax = 300_000  # taxes went up
    r = analyze(lo, date(2025, 1, 1), sv.rules(lo))
    assert r["new_monthly_deposit"] == ceil_div(420_000, 12) == 35_000
    assert r["shortage"] > 0 and r["shortage_monthly"] == ceil_div(r["shortage"], 12)
    assert r["low_point"] == min(row["balance"] for row in r["projection"])
    assert r["new_total_payment"] == lo.pi_payment + 35_000 + r["shortage_monthly"]


def test_shortage_lump_sum_option():
    sv, lo = make_book(escrow_balance=0)
    r = analyze(lo, date(2025, 1, 1), sv.rules(lo), "lump")
    assert r["shortage_monthly"] == r["shortage"] and r["spread_months"] == 1


def test_deficiency_reported_when_low_point_negative():
    sv, lo = make_book(escrow_balance=0)
    r = analyze(lo, date(2025, 1, 1), sv.rules(lo))
    assert r["low_point"] < 0 and r["deficiency"] == -r["low_point"]
    assert r["shortage"] == r["cushion"] - r["low_point"]


def test_surplus_refunded_when_current_and_over_50():
    sv, lo = make_book(escrow_balance=400_000)
    r = analyze(lo, date(2025, 1, 1), sv.rules(lo))
    assert r["surplus"] >= 5_000 and r["refund"] == r["surplus"]
    before = lo.escrow_balance
    apply_analysis(sv, lo, r, date(2024, 12, 31))
    assert lo.escrow_balance == before - r["refund"]
    assert ledger_ties(sv, lo)


def test_small_surplus_kept():
    sv, lo = make_book()
    probe = analyze(lo, date(2025, 1, 1), sv.rules(lo))
    lo.escrow_balance += probe["cushion"] - probe["low_point"] + 3_000  # $30 over the cushion
    r = analyze(lo, date(2025, 1, 1), sv.rules(lo))
    assert r["surplus"] == 3_000 and r["refund"] == 0 and "kept" in r["action"]


def test_surplus_not_refunded_when_delinquent():
    sv, lo = make_book(escrow_balance=500_000)
    r = analyze(lo, date(2025, 3, 1), sv.rules(lo))  # Jan and Feb unpaid
    assert r["deposits_owed_on_unpaid_installments"] == 60_000
    assert r["surplus"] > 0 and r["refund"] == 0


def test_notice_preview_mentions_new_payment():
    sv, lo = make_book(escrow_balance=0)
    r = analyze(lo, date(2025, 1, 1), sv.rules(lo))
    text = " ".join(r["notice"])
    assert "L1" in text and "shortage" in text and "$" in text


def test_payoff_per_diem_365():
    sv, lo = make_book()
    q = sv.payoff_quote(lo, date(2025, 1, 15))
    # interest from 2024-12-01 (the unpaid January installment's period) through 2025-01-14: 45 days
    assert q["interest_from"] == "2024-12-01" and q["interest_days"] == 45
    assert q["interest"] == round(20_000_000 * 0.06 * 45 / 365)
    assert q["per_diem"] == 3_288
    assert q["total"] == 20_000_000 + q["interest"] + q["release_fee"]
    assert q["escrow_refund_after_payoff"] == 120_000


def test_payoff_per_diem_360_and_credits_and_fees():
    sv, lo = make_book(InvestorRules(day_count=360))
    sv.apply_payment("L1", 149_910, date(2025, 1, 3))
    sv.apply_payment("L1", 50_000, date(2025, 2, 3))  # held in suspense
    lo.late_fees = 2_500
    q = sv.payoff_quote(lo, date(2025, 2, 11))
    assert q["interest_days"] == 41 and q["per_diem"] == 3_330
    assert q["interest"] == (lo.principal * 6000 * 41 * 2 + 36_000_000) // (2 * 36_000_000)
    assert q["total"] == lo.principal + q["interest"] + 2_500 + q["release_fee"] - 50_000


def test_good_through_table_grows_by_per_diem():
    sv, lo = make_book()
    q = sv.payoff_quote(lo, date(2025, 1, 15))
    diffs = [q["per_day_after"][0]["total"] - q["total"]] + [
        b["total"] - a["total"] for a, b in zip(q["per_day_after"], q["per_day_after"][1:], strict=False)
    ]
    assert all(abs(d - q["per_diem"]) <= 1 for d in diffs)


def test_process_payoff_zeroes_loan_and_refunds_escrow():
    sv, lo = make_book()
    t = sv.process_payoff(lo, date(2025, 1, 15))
    assert lo.status == "paid_off" and lo.principal == 0 and lo.escrow_balance == 0
    assert t.splits["principal"] == 20_000_000
    assert lo.history[-1].kind == "escrow_refund" and lo.history[-1].amount == -120_000
    assert ledger_ties(sv, lo)


def test_payoff_recovers_escrow_advance():
    sv, lo = make_book()
    sv.disburse(lo, "property tax", 200_000, date(2025, 1, 10))
    q = sv.payoff_quote(lo, date(2025, 1, 15))
    assert q["escrow_advance"] == 80_000
    sv.process_payoff(lo, date(2025, 1, 15))
    assert lo.escrow_balance == 0 and ledger_ties(sv, lo)
