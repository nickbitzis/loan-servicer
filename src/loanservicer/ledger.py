"""Loan-level double-entry ledger.

Every money event posts lines that sum to zero (debit positive, credit
negative). Balances are kept per (loan, account) and per (pool, month,
account), so reports can be checked against the ledger without replaying
history.

Accounts:
  custodial_cash      cash held for investors and for taxes and insurance
  loan_upb            unpaid principal owed by the borrower (debit balance)
  investor_funding    the pool's funding of the loans (credit)
  investor_interest   interest collected, net of fees, owed to the investor
  servicing_fee       servicing fee income retained from interest
  gfee_payable        guarantee fee owed to the guarantor
  late_fee_income     late and NSF fees collected (servicer income)
  payoff_fee_income   release and recording fees collected at payoff
  escrow              escrow held for the borrower (credit; debit = advance)
  suspense            partial payments held (credit)
  unapplied           overpayments held (credit)
  capitalized_interest  delinquent interest moved into principal by a modification
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from .money import ym


class LedgerError(Exception):
    pass


class Ledger:
    def __init__(self):
        self.balances: dict[tuple[str, str], int] = defaultdict(int)
        self.monthly: dict[tuple[str, str, str], int] = defaultdict(int)
        self.total = 0
        self.entries = 0

    def post(self, loan_id: str, pool_id: str, on: date, lines: dict[str, int]) -> None:
        s = 0
        for v in lines.values():
            if isinstance(v, bool) or not isinstance(v, int):
                raise LedgerError("ledger amounts must be int cents")
            s += v
        if s != 0:
            raise LedgerError(f"unbalanced posting for {loan_id} on {on}: {lines} sums to {s}")
        month = ym(on)
        for acct, v in lines.items():
            if v == 0:
                continue
            self.balances[(loan_id, acct)] += v
            self.monthly[(pool_id, month, acct)] += v
            self.total += v
            self.entries += 1

    def balance(self, loan_id: str, account: str) -> int:
        return self.balances.get((loan_id, account), 0)

    def pool_month(self, pool_id: str, month: str, account: str) -> int:
        return self.monthly.get((pool_id, month, account), 0)


def payment_lines(amount: int, s: dict[str, int]) -> dict[str, int]:
    """Ledger lines for an applied payment (or, negated, its reversal)."""
    interest = s.get("interest", 0)
    sfee = s.get("servicing_fee", 0)
    gfee = s.get("gfee", 0)
    return {
        "custodial_cash": amount,
        "loan_upb": -(s.get("principal", 0) + s.get("curtailment", 0)),
        "investor_interest": -(interest - sfee - gfee),
        "servicing_fee": -sfee,
        "gfee_payable": -gfee,
        "late_fee_income": -s.get("late_fees", 0),
        "payoff_fee_income": -s.get("payoff_fees", 0),
        "escrow": -(s.get("escrow", 0) + s.get("escrow_shortage", 0)),
        "suspense": -s.get("suspense", 0),
        "unapplied": -s.get("unapplied", 0),
    }
