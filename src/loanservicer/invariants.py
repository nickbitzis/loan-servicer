"""Month-end invariants. Each returns a list of failure strings; empty means it holds."""

from __future__ import annotations

from .servicing import Servicer


def check_loans(sv: Servicer) -> tuple[int, list[str]]:
    failures: list[str] = []
    checked = 0
    led = sv.ledger
    for lo in sv.loans.values():
        checked += 1
        lid = lo.loan_id
        for name in ("principal", "suspense", "unapplied", "late_fees", "shortage_outstanding", "remaining_term"):
            if getattr(lo, name) < 0:
                failures.append(f"{lid}: {name} is negative ({getattr(lo, name)})")
        if led.balance(lid, "loan_upb") != lo.principal:
            failures.append(f"{lid}: ledger principal {led.balance(lid, 'loan_upb')} != loan {lo.principal}")
        if -led.balance(lid, "escrow") != lo.escrow_balance:
            failures.append(f"{lid}: ledger escrow {-led.balance(lid, 'escrow')} != loan {lo.escrow_balance}")
        if -led.balance(lid, "suspense") != lo.suspense:
            failures.append(f"{lid}: ledger suspense != loan suspense")
        if -led.balance(lid, "unapplied") != lo.unapplied:
            failures.append(f"{lid}: ledger unapplied != loan unapplied")
        if lo.status == "paid_off" and (lo.principal or lo.escrow_balance or lo.suspense or lo.unapplied):
            failures.append(f"{lid}: paid off loan still carries a balance")
    if led.total != 0:
        failures.append(f"ledger does not balance: total {led.total}")
    return checked, failures
