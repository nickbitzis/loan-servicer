from datetime import date

from conftest import make_book

from loanservicer.delinquency import BUCKETS
from loanservicer.invariants import check_loans
from loanservicer.models import InvestorRules
from loanservicer.reporting import loan_level_totals, reconcile, remittance
from loanservicer.simulate import SimConfig, Simulation


def book_with_month():
    sv, lo = make_book(InvestorRules(servicing_fee_mp=250, gfee_mp=450))
    sv.upb_begin[("P", "2025-01")] = sv.pool_upb("P")
    sv.scheduled[("P", "2025-01")] = {"principal": 19_910, "interest": 100_000, "loans": 1}
    sv.apply_payment("L1", 149_910 + 30_000, date(2025, 1, 5))  # installment plus a $300 curtailment
    sv.disburse(lo, "hazard insurance", 120_000, date(2025, 1, 10))
    return sv, lo


def test_remittance_math():
    sv, _ = book_with_month()
    r = remittance(sv, "P", "2025-01")
    assert r["actual_scheduled_principal"] == 19_910 and r["curtailments"] == 30_000
    assert r["actual_principal"] == 49_910
    assert r["servicing_fee"] == 4_167 and r["gfee"] == 7_500
    assert r["net_interest"] == 100_000 - 4_167 - 7_500
    assert r["remittance"] == 49_910 + 100_000 - 4_167 - 7_500
    assert r["scheduled_minus_collected_pi"] == 0


def test_reconciliation_ties_out_to_the_cent():
    sv, _ = book_with_month()
    rec = reconcile(sv, "P", "2025-01")
    assert rec["ties_out"], rec["checks"]
    assert rec["upb_end"] == 20_000_000 - 49_910
    assert len(rec["checks"]) == 7


def test_reconciliation_catches_a_ledger_break():
    sv, _ = book_with_month()
    sv.ledger.monthly[("P", "2025-01", "investor_interest")] += 1  # one cent off
    rec = reconcile(sv, "P", "2025-01")
    assert not rec["ties_out"]
    bad = [c for c in rec["checks"] if not c["ok"]]
    assert len(bad) == 1 and bad[0]["diff"] == -1


def test_upb_rollforward_includes_capitalization():
    sv, lo = make_book()
    sv.upb_begin[("P", "2025-04")] = sv.pool_upb("P")
    sv.modify_loan(lo, date(2025, 4, 1), 4500, 480)
    t = loan_level_totals(sv, "P", "2025-04")
    assert t["capitalized"] == 300_000
    assert reconcile(sv, "P", "2025-04")["ties_out"]


def test_reversal_nets_out_of_remittance():
    sv, lo = make_book()
    t = sv.apply_payment("L1", 149_910, date(2025, 1, 2))
    sv.reverse_payment("L1", t.txn_id, date(2025, 1, 6))
    r = remittance(sv, "P", "2025-01")
    assert r["actual_principal"] == 0 and r["actual_interest"] == 0 and r["remittance"] == 0


def test_invariant_checker_flags_negative_and_mismatch():
    sv, lo = make_book()
    assert check_loans(sv)[1] == []
    lo.suspense = -1
    fails = check_loans(sv)[1]
    assert any("suspense is negative" in f for f in fails)
    assert any("ledger suspense" in f for f in fails)


def test_simulation_invariants_hold_every_month(small_sim):
    for mo in small_sim.months:
        assert mo["invariants"]["failure_count"] == 0, mo["invariants"]["failures"]
        assert mo["invariants"]["loans_checked"] == 500


def test_simulation_remittance_ties_every_pool_every_month(small_sim):
    for mo in small_sim.months:
        for pid, rec in mo["recon"].items():
            assert rec["ties_out"], (mo["month"], pid, rec["checks"])


def test_simulation_produces_every_behavior(small_sim):
    c = small_sim.totals
    for key in (
        "payments",
        "nsf_reversals",
        "late_fees_assessed",
        "payoffs",
        "recasts",
        "modifications",
        "arm_resets",
        "escrow_analyses",
        "escrow_shortages",
        "forbearance_starts",
        "plans_offered",
        "loss_mit_referrals",
        "payments_to_suspense",
        "curtailments",
        "disbursements",
    ):
        assert c[key] > 0, key


def test_simulation_balances_never_negative(small_sim):
    for lo in small_sim.sv.loans.values():
        assert lo.principal >= 0 and lo.suspense >= 0 and lo.unapplied >= 0 and lo.late_fees >= 0


def test_simulation_buckets_and_rolls_are_consistent(small_sim):
    for prev, mo in zip(small_sim.months, small_sim.months[1:], strict=False):
        counts = mo["roll"]["counts"]
        # every loan active last month is in exactly one cell of this month's roll matrix
        assert sum(sum(row.values()) for row in counts.values()) == prev["active_loans"]
        for b in BUCKETS:
            assert sum(counts[a][b] for a in BUCKETS) == mo["buckets"][b]


def test_simulation_is_deterministic():
    a = Simulation(SimConfig(loans=120, months=4, seed=3))
    b = Simulation(SimConfig(loans=120, months=4, seed=3))
    sa, sb = a.run(), b.run()
    sa.pop("elapsed_seconds"), sb.pop("elapsed_seconds")
    assert sa == sb
    c = Simulation(SimConfig(loans=120, months=4, seed=4)).run()
    c.pop("elapsed_seconds")
    assert c != sa


def test_hardship_cohort_enters_forbearance_then_modifies(small_sim):
    mods = [lo for lo in small_sim.sv.loans.values() if any(e["event"] == "modification" for e in lo.events)]
    fb = [lo for lo in small_sim.sv.loans.values() if any(e["event"] == "forbearance_start" for e in lo.events)]
    assert fb and mods
    for lo in mods:
        assert lo.state in ("modified", "loss_mit", "payment_plan", "normal") or lo.status == "paid_off"
        assert lo.remaining_term <= 480
