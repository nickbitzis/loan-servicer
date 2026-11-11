from __future__ import annotations

from datetime import date

import pytest

from loanservicer.amortization import level_payment
from loanservicer.models import Borrower, InvestorRules, Loan, Pool
from loanservicer.servicing import Servicer
from loanservicer.simulate import SimConfig, Simulation

START = date(2025, 1, 1)


def make_book(rules: InvestorRules | None = None, **loan_kw) -> tuple[Servicer, Loan]:
    """One current loan: $200,000 at 6.000% for 360 months, $300 a month escrow, next due 2025-01-01."""
    rules = rules or InvestorRules()
    pools = {"P": Pool("P", "test pool", rules)}
    index = {k: 4000 for k in range(2000 * 12, 2040 * 12)}
    sv = Servicer(pools, index)
    kw = dict(
        loan_id="L1",
        pool_id="P",
        borrower=Borrower("Test Borrower", "TX", 720),
        product="fixed30",
        orig_balance=20_000_000,
        orig_rate_mp=6000,
        term=360,
        first_due=START,
        principal=20_000_000,
        rate_mp=6000,
        pi_payment=level_payment(20_000_000, 6000, 360),
        next_due=START,
        installments_paid=0,
        remaining_term=360,
        escrow_balance=120_000,
        escrow_monthly=30_000,
        annual_tax=240_000,
        annual_ins=120_000,
        tax_months=(6, 12),
        ins_month=3,
        analysis_month=1,
    )
    kw.update(loan_kw)
    loan = Loan(**kw)
    sv.board(loan, date(2024, 12, 31))
    return sv, loan


def ledger_ties(sv: Servicer, loan: Loan) -> bool:
    led = sv.ledger
    return (
        led.total == 0
        and led.balance(loan.loan_id, "loan_upb") == loan.principal
        and -led.balance(loan.loan_id, "escrow") == loan.escrow_balance
        and -led.balance(loan.loan_id, "suspense") == loan.suspense
        and -led.balance(loan.loan_id, "unapplied") == loan.unapplied
    )


@pytest.fixture(scope="session")
def small_sim() -> Simulation:
    sim = Simulation(SimConfig(loans=500, months=12, seed=7))
    sim.run()
    return sim
