"""Domain model: investor rules, pools, borrowers, loans and transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .amortization import ArmTerms
from .money import add_months, monthly_interest

COMPONENTS = ("late_fees", "escrow_shortage", "interest", "principal", "escrow")
CONTRACTUAL = ("escrow_shortage", "interest", "principal", "escrow")


@dataclass(frozen=True)
class InvestorRules:
    """Per-investor servicing rules. Illustrative, not any agency's guidelines."""

    order: tuple[str, ...] = ("interest", "principal", "escrow", "escrow_shortage", "late_fees")
    partial_policy: str = "suspense"  # "suspense": hold until a full installment; "apply": apply in order
    excess: str = "curtailment"  # what happens to money left after all due installments
    pay_ahead: bool = False  # True: extra funds pay next month's installment first if they cover it
    grace_days: int = 15
    late_fee_bp: int = 500  # basis points of the P&I payment
    late_fee_cap: int = 10_000  # cents
    nsf_fee: int = 2_500
    servicing_fee_mp: int = 250  # 0.25% a year
    gfee_mp: int = 0
    day_count: int = 365  # per diem basis for payoffs
    cushion_months: int = 2  # escrow cushion, at most 1/6 of annual disbursements
    shortage_spread_months: int = 12
    surplus_refund_min: int = 5_000  # refund surpluses of $50 or more on current loans
    release_fee: int = 4_500  # lien release recording fee on payoff

    def __post_init__(self):
        if sorted(self.order) != sorted(COMPONENTS):
            raise ValueError(f"order must be a permutation of {COMPONENTS}")
        if self.partial_policy not in ("suspense", "apply"):
            raise ValueError("partial_policy must be suspense or apply")
        if self.excess not in ("curtailment", "unapplied"):
            raise ValueError("excess must be curtailment or unapplied")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class Pool:
    pool_id: str
    name: str
    rules: InvestorRules


@dataclass
class Borrower:
    name: str
    state: str
    fico: int
    contact_pref: str = "phone"  # phone, email, mail
    cease_contact: bool = False
    bankruptcy: bool = False


@dataclass(slots=True)
class Txn:
    txn_id: str
    loan_id: str
    pool_id: str
    on: date
    kind: str  # payment, reversal, payoff, disbursement, escrow_refund, capitalization, fee_assessed, fee_waived
    amount: int  # cash in (+) or out (-) of the custodial account; 0 for memo items
    method: str = ""
    splits: dict[str, int] = field(default_factory=dict)
    installments: list[str] = field(default_factory=list)
    note: str = ""
    reversed_by: str | None = None
    reverses: str | None = None
    before: dict | None = None  # state needed to unwind this payment (kept for the NSF window)

    def to_dict(self) -> dict:
        return {
            "txn_id": self.txn_id,
            "on": self.on.isoformat(),
            "kind": self.kind,
            "amount": self.amount,
            "method": self.method,
            "splits": dict(self.splits),
            "installments": list(self.installments),
            "note": self.note,
            "reversed_by": self.reversed_by,
            "reverses": self.reverses,
        }


@dataclass
class Loan:
    loan_id: str
    pool_id: str
    borrower: Borrower
    product: str
    orig_balance: int
    orig_rate_mp: int
    term: int
    first_due: date
    io_months: int = 0
    arm: ArmTerms | None = None
    # live servicing state
    principal: int = 0
    rate_mp: int = 0
    pi_payment: int = 0
    next_due: date = date(2000, 1, 1)
    installments_paid: int = 0
    remaining_term: int = 0
    escrowed: bool = True
    escrow_balance: int = 0
    escrow_monthly: int = 0
    shortage_outstanding: int = 0
    shortage_monthly: int = 0
    annual_tax: int = 0
    annual_ins: int = 0
    tax_months: tuple[int, int] = (6, 12)
    ins_month: int = 1
    analysis_month: int = 1
    suspense: int = 0
    unapplied: int = 0
    late_fees: int = 0
    late_fee_assessed: set = field(default_factory=set)
    inst_state: dict | None = None  # {"due", "req": {...}, "paid": {...}} for the installment in progress
    status: str = "active"  # active, paid_off
    state: str = "normal"  # normal, payment_plan, forbearance, loss_mit, modified
    autopay: bool = False
    behavior: str = "reliable"
    nsf_count: int = 0
    resets_done: int = 0
    plan: dict | None = None
    forbearance: dict | None = None
    contact_log: list = field(default_factory=list)
    history: list[Txn] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    schedule_version: int = 0  # bumps on recast, modification and rate reset

    # installment math --------------------------------------------------
    def in_io(self) -> bool:
        return self.installments_paid < self.io_months

    def installment_requirement(self) -> dict[str, int]:
        """Contractual amounts for the next due installment, from the current state."""
        interest = monthly_interest(self.principal, self.rate_mp)
        if self.in_io():
            principal = 0
        elif self.remaining_term <= 1:
            principal = self.principal
        else:
            principal = min(max(self.pi_payment - interest, 0), self.principal)
        return {
            "escrow_shortage": min(self.shortage_monthly, self.shortage_outstanding) if self.escrowed else 0,
            "interest": interest,
            "principal": principal,
            "escrow": self.escrow_monthly if self.escrowed else 0,
            "_basis": self.principal,
            "_rate": self.rate_mp,
        }

    def current_installment(self) -> dict:
        if self.inst_state is None or self.inst_state["due"] != self.next_due:
            req = self.installment_requirement()
            self.inst_state = {"due": self.next_due, "req": req, "paid": dict.fromkeys(CONTRACTUAL, 0)}
        return self.inst_state

    def installment_remaining(self) -> dict[str, int]:
        st = self.current_installment()
        return {c: st["req"][c] - st["paid"][c] for c in CONTRACTUAL}

    def monthly_payment(self) -> int:
        r = self.installment_requirement()
        return sum(r[c] for c in CONTRACTUAL)

    def installments_total(self, k: int) -> int:
        """Exact total of the next k contractual installments (interest falls as principal is paid)."""
        if k <= 0:
            return 0
        rem = self.installment_remaining()
        total = sum(rem.values())
        bal = self.principal - rem["principal"]
        shortage_left = self.shortage_outstanding - rem["escrow_shortage"]
        paid = self.installments_paid + 1
        left = self.remaining_term - 1
        for _ in range(k - 1):
            if left <= 0 or bal <= 0:
                break
            interest = monthly_interest(bal, self.rate_mp)
            if paid < self.io_months:
                principal = 0
            elif left <= 1:
                principal = bal
            else:
                principal = min(max(self.pi_payment - interest, 0), bal)
            sh = min(self.shortage_monthly, shortage_left) if self.escrowed else 0
            shortage_left -= sh
            total += interest + principal + (self.escrow_monthly if self.escrowed else 0) + sh
            bal -= principal
            paid += 1
            left -= 1
        return total

    def unpaid_dues(self, as_of: date) -> list[date]:
        out = []
        d = self.next_due
        while d <= as_of and len(out) < 600:
            out.append(d)
            d = add_months(d, 1)
        return out

    def summary(self) -> dict:
        return {
            "loan_id": self.loan_id,
            "pool_id": self.pool_id,
            "product": self.product,
            "borrower": self.borrower.name,
            "state_code": self.borrower.state,
            "principal": self.principal,
            "rate_mp": self.rate_mp,
            "next_due": self.next_due.isoformat(),
            "status": self.status,
            "servicing_state": self.state,
            "behavior": self.behavior,
        }
