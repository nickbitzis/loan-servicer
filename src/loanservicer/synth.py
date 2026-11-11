"""Seeded synthetic portfolio, investor pools and a synthetic rate index.

Tidewell Loan Servicing, its pools, borrowers and the index are all made up.
"""

from __future__ import annotations

import random
from datetime import date

from .amortization import ArmTerms, level_payment, round_to_eighth
from .models import Borrower, InvestorRules, Loan, Pool
from .money import add_months, ceil_div, month_index, monthly_interest

SERVICER = "Tidewell Loan Servicing"
SIM_START = date(2024, 11, 1)

POOLS: dict[str, Pool] = {
    "TW-AG-21": Pool(
        "TW-AG-21",
        "Agency-style fixed pool",
        InvestorRules(
            order=("interest", "principal", "escrow", "escrow_shortage", "late_fees"),
            partial_policy="suspense",
            excess="curtailment",
            servicing_fee_mp=250,
            gfee_mp=450,
        ),
    ),
    "TW-PL-22": Pool(
        "TW-PL-22",
        "Private label fixed pool",
        InvestorRules(
            order=("late_fees", "interest", "principal", "escrow", "escrow_shortage"),
            partial_policy="apply",
            excess="curtailment",
            pay_ahead=True,
            late_fee_bp=600,
            servicing_fee_mp=375,
            gfee_mp=0,
            day_count=360,
        ),
    ),
    "TW-ARM-23": Pool(
        "TW-ARM-23",
        "ARM pool",
        InvestorRules(
            order=("escrow_shortage", "interest", "principal", "escrow", "late_fees"),
            partial_policy="suspense",
            excess="unapplied",
            servicing_fee_mp=375,
            gfee_mp=0,
        ),
    ),
    "TW-BAL": Pool(
        "TW-BAL",
        "Balance sheet portfolio",
        InvestorRules(grace_days=10, servicing_fee_mp=250, gfee_mp=0, cushion_months=1),
    ),
}

# product: (weight, term, io_months, arm (initial, reset, caps) or None)
PRODUCTS = {
    "fixed30": (0.52, 360, 0, None),
    "fixed15": (0.14, 180, 0, None),
    "fixed20": (0.06, 240, 0, None),
    "fixed30_io10": (0.04, 360, 120, None),
    "arm5_6": (0.12, 360, 0, (60, 6, 2000, 1000, 5000)),
    "arm7_6": (0.08, 360, 0, (84, 6, 5000, 1000, 5000)),
    "arm10_6_io": (0.04, 360, 120, (120, 6, 5000, 1000, 5000)),
}

BEHAVIORS = {
    "reliable": 0.56,
    "manual_ontime": 0.17,
    "sometimes_late": 0.10,
    "partial": 0.03,
    "chronic": 0.035,
    "prepayer": 0.05,
    "recaster": 0.015,
    "hardship": 0.03,
    "nsf_prone": 0.01,
}

FIRST = [
    "Avery",
    "Jordan",
    "Riley",
    "Casey",
    "Morgan",
    "Taylor",
    "Quinn",
    "Rowan",
    "Harper",
    "Emerson",
    "Logan",
    "Parker",
    "Reese",
    "Skyler",
    "Dana",
    "Jamie",
    "Kendall",
    "Sawyer",
    "Elliot",
    "Marlow",
    "Hollis",
    "Arden",
]
LAST = [
    "Alder",
    "Brook",
    "Calloway",
    "Dunmore",
    "Ellison",
    "Fairbanks",
    "Garrow",
    "Hale",
    "Ingram",
    "Juniper",
    "Kessler",
    "Larkin",
    "Mercer",
    "Northcott",
    "Orwell",
    "Pell",
    "Quarry",
    "Rendell",
    "Stroud",
    "Thorne",
    "Underhill",
    "Vance",
    "Whitlock",
    "Yardley",
]
STATES = ["CA", "TX", "FL", "NY", "IL", "PA", "OH", "GA", "NC", "MI", "NJ", "VA", "WA", "AZ", "MA", "CO"]
TAX_MONTHS = [(4, 10), (6, 12), (3, 9), (1, 7)]

# anchor points (year, month, mp) for the synthetic index; values in between are interpolated with noise
INDEX_ANCHORS = [
    (2012, 1, 200),
    (2016, 1, 300),
    (2019, 1, 2400),
    (2020, 4, 100),
    (2021, 12, 100),
    (2023, 8, 5300),
    (2024, 8, 5300),
    (2025, 6, 4300),
    (2026, 6, 3600),
    (2028, 12, 3400),
]


def index_series(seed: int = 7) -> dict[int, int]:
    """Monthly synthetic index in mp, keyed by month_index."""
    rng = random.Random(seed * 101 + 3)
    out: dict[int, int] = {}
    for (y0, m0, v0), (y1, m1, v1) in zip(INDEX_ANCHORS, INDEX_ANCHORS[1:], strict=False):
        a, b = y0 * 12 + m0 - 1, y1 * 12 + m1 - 1
        for k in range(a, b):
            base = v0 + (v1 - v0) * (k - a) // (b - a)
            out[k] = max(50, base + rng.randint(-60, 60))
    last = INDEX_ANCHORS[-1]
    out[last[0] * 12 + last[1] - 1] = last[2]
    return out


def _pick(rng: random.Random, weights: dict) -> str:
    r = rng.random()
    acc = 0.0
    for k, w in weights.items():
        acc += w if isinstance(w, float) else w[0]
        if r < acc:
            return k
    return next(iter(weights))


def _escrow_start(loan: Loan, start: date, cushion_months: int) -> tuple[int, int]:
    """Monthly deposit and a starting balance whose 12 month low point equals the cushion."""
    from .escrow import projected_disbursements

    disb = projected_disbursements(loan, start)
    annual = sum(x["amount"] for x in disb)
    monthly = ceil_div(annual, 12)
    cushion = min(monthly * cushion_months, annual // 6)
    bal, low = 0, None
    for i in range(12):
        d = add_months(start, i).isoformat()[:7]
        bal += monthly - sum(x["amount"] for x in disb if x["date"][:7] == d)
        low = bal if low is None else min(low, bal)
    return monthly, cushion - (low or 0)


def make_loan(i: int, seed: int, index: dict[int, int], start: date = SIM_START) -> Loan:
    rng = random.Random(seed * 1_000_003 + i)
    product = _pick(rng, PRODUCTS)
    _, term, io, arm_spec = PRODUCTS[product]
    arm = None
    if arm_spec:
        initial, reset, c1, c2, c3 = arm_spec
        arm = ArmTerms(2750, initial, reset, c1, c2, c3, 2750)
        if rng.random() < 0.65:
            seasoning = rng.randint(initial - 23, initial - 1)
        else:
            seasoning = rng.randint(1, max(1, initial - 25))
    elif io:
        seasoning = rng.randint(96, 119) if rng.random() < 0.6 else rng.randint(1, 95)
    else:
        seasoning = min(term - 24, int(rng.triangular(1, 120, 18)))
    first_due = add_months(start, -seasoning)
    orig_idx = index.get(month_index(first_due) - 2, 2000)
    if arm:
        rate = round_to_eighth(max(2500, orig_idx + arm.margin_mp - 750 + rng.randint(-250, 250)))
    else:
        spread = 1750 if term == 360 else 1250
        rate = round_to_eighth(max(2625, orig_idx + spread + rng.randint(-300, 400)))
    orig = int(rng.lognormvariate(12.55, 0.45)) // 100 * 10_000
    orig = max(6_000_000, min(orig, 120_000_000))
    pi = level_payment(orig, rate, term - io)
    bal = orig
    for k in range(seasoning):
        if k >= io:
            bal -= min(max(pi - monthly_interest(bal, rate), 0), bal)
    behavior = _pick(rng, BEHAVIORS)
    fico = max(600, min(830, int(rng.gauss(735, 45))))
    if behavior in ("chronic", "hardship"):
        fico = max(600, fico - 60)
    borrower = Borrower(
        name=f"{rng.choice(FIRST)} {rng.choice(LAST)}",
        state=rng.choice(STATES),
        fico=fico,
        contact_pref=rng.choices(["phone", "email", "mail"], [0.6, 0.3, 0.1])[0],
        cease_contact=rng.random() < 0.01,
        bankruptcy=behavior == "chronic" and rng.random() < 0.08,
    )
    if arm:
        pool_id = "TW-ARM-23" if rng.random() < 0.8 else "TW-BAL"
    else:
        r = rng.random()
        pool_id = "TW-AG-21" if r < 0.65 else ("TW-PL-22" if r < 0.88 else "TW-BAL")
    loan = Loan(
        loan_id=f"TW-{100000 + i}",
        pool_id=pool_id,
        borrower=borrower,
        product=product,
        orig_balance=orig,
        orig_rate_mp=rate,
        term=term,
        first_due=first_due,
        io_months=io,
        arm=arm,
        principal=bal,
        rate_mp=rate,
        pi_payment=pi,
        next_due=start,
        installments_paid=seasoning,
        remaining_term=term - seasoning,
        autopay=(behavior == "reliable" and rng.random() < 0.75) or behavior == "nsf_prone",
        behavior=behavior,
    )
    loan.escrowed = rng.random() < 0.85
    if loan.escrowed:
        value = orig * 100 // rng.randint(60, 95)
        loan.annual_tax = value * rng.randint(80, 220) // 10_000
        loan.annual_ins = value * rng.randint(30, 70) // 10_000
        loan.tax_months = rng.choice(TAX_MONTHS)
        loan.ins_month = first_due.month
        loan.analysis_month = first_due.month
        monthly, balance = _escrow_start(loan, start, POOLS[pool_id].rules.cushion_months)
        loan.escrow_monthly = monthly
        loan.escrow_balance = max(0, balance)
    else:
        loan.analysis_month = first_due.month
    if behavior == "hardship":
        fb = rng.randint(4, 8)
        loan.forbearance = {"start_month": fb, "end_month": fb + 6, "status": "planned"}
    return loan


def make_portfolio(n: int, seed: int = 7, start: date = SIM_START) -> tuple[list[Loan], dict[int, int]]:
    index = index_series(seed)
    return [make_loan(i, seed, index, start) for i in range(n)], index
