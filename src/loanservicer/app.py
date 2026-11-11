"""FastAPI service over a simulated servicing book."""

from __future__ import annotations

import os
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, StrictInt

from . import __version__
from .amortization import schedule
from .delinquency import BUCKETS, bucket, collections_queue, days_past_due, months_behind, roll_forward_bp
from .escrow import analyze
from .logs import get_logger, log_event
from .metrics import Metrics
from .models import Loan
from .reporting import delinquency_report, reconcile, remittance
from .servicing import ServicingError
from .simulate import SimConfig, Simulation, amount_due, series
from .synth import SERVICER

APP_ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).parent / "static"


class PaymentIn(BaseModel):
    amount_cents: StrictInt
    on: date | None = None
    method: str = "manual"


class ReverseIn(BaseModel):
    on: date | None = None
    reason: str = "NSF"


def loan_detail(sim: Simulation, lo: Loan, as_of: date) -> dict:
    io_left = max(0, lo.io_months - lo.installments_paid)
    rows = []
    if lo.status == "active" and lo.remaining_term > 0:
        pmt = lo.pi_payment if io_left == 0 else None
        rows = [
            r.to_dict()
            for r in schedule(
                lo.principal, lo.rate_mp, lo.remaining_term, lo.next_due, io_left, pmt, start_n=lo.installments_paid + 1
            )
        ]
    return {
        **lo.summary(),
        "borrower": {
            "name": lo.borrower.name,
            "state": lo.borrower.state,
            "fico": lo.borrower.fico,
            "contact_pref": lo.borrower.contact_pref,
            "cease_contact": lo.borrower.cease_contact,
            "bankruptcy": lo.borrower.bankruptcy,
        },
        "terms": {
            "orig_balance": lo.orig_balance,
            "orig_rate_mp": lo.orig_rate_mp,
            "term": lo.term,
            "first_due": lo.first_due.isoformat(),
            "io_months": lo.io_months,
            "arm": lo.arm.to_dict() if lo.arm else None,
        },
        "pi_payment": lo.pi_payment,
        "monthly_payment": lo.monthly_payment() if lo.status == "active" else 0,
        "amount_due": amount_due(lo, as_of) if lo.status == "active" else 0,
        "installments_paid": lo.installments_paid,
        "remaining_term": lo.remaining_term,
        "dpd": days_past_due(lo, as_of),
        "months_behind": months_behind(lo, as_of),
        "bucket": bucket(lo, as_of),
        "suspense": lo.suspense,
        "unapplied": lo.unapplied,
        "late_fees": lo.late_fees,
        "escrow": {
            "escrowed": lo.escrowed,
            "balance": lo.escrow_balance,
            "monthly": lo.escrow_monthly,
            "shortage_outstanding": lo.shortage_outstanding,
            "shortage_monthly": lo.shortage_monthly,
            "annual_tax": lo.annual_tax,
            "annual_insurance": lo.annual_ins,
            "tax_months": list(lo.tax_months),
            "insurance_month": lo.ins_month,
            "analysis_month": lo.analysis_month,
        },
        "plan": lo.plan,
        "events": lo.events,
        "history": [t.to_dict() for t in reversed(lo.history)],
        "schedule": rows,
    }


def pick_examples(sim: Simulation) -> list[dict]:
    """A few loans that show each feature, for the UI picker."""
    want = {
        "modification after forbearance": lambda lo: any(e["event"] == "modification" for e in lo.events),
        "NSF reversal": lambda lo: any(t.kind == "reversal" for t in lo.history),
        "recast after a large curtailment": lambda lo: any(e["event"] == "recast" for e in lo.events),
        "ARM reset limited by a cap": lambda lo: any(
            e["event"] == "arm_reset" and e["limited_by"] != "none" for e in lo.events
        ),
        "partial payments in suspense": lambda lo: any(t.splits.get("suspense", 0) > 0 for t in lo.history),
        "120+ days, loss mitigation": lambda lo: lo.state == "loss_mit" and lo.status == "active",
        "escrow shortage spread": lambda lo: lo.shortage_monthly > 0 and lo.status == "active",
    }
    out = []
    for label, pred in want.items():
        for lo in sim.sv.loans.values():
            if pred(lo):
                out.append({"label": label, "loan_id": lo.loan_id})
                break
    return out


def create_app(
    loans: int | None = None, months: int | None = None, seed: int | None = None, audit_path: str | None = None
) -> FastAPI:
    cfg = SimConfig(
        loans=loans or int(os.environ.get("LS_LOANS", "5000")),
        months=months or int(os.environ.get("LS_MONTHS", "24")),
        seed=seed or int(os.environ.get("LS_SEED", "7")),
        audit_path=audit_path or os.environ.get("LS_AUDIT", str(APP_ROOT / "var" / "audit.jsonl")),
    )
    log = get_logger()
    t0 = time.perf_counter()
    sim = Simulation(cfg)
    summary = sim.run()
    log_event(
        log,
        "simulation complete",
        loans=cfg.loans,
        months=cfg.months,
        seconds=round(time.perf_counter() - t0, 2),
        invariants_hold=summary["all_invariants_hold"],
        ties_out=summary["remittance_ties_out"],
    )
    sv = sim.sv
    last_month = sim.months[-1]
    today = date.fromisoformat(last_month["as_of"]) + timedelta(days=1)
    examples = pick_examples(sim)
    metrics = Metrics()
    app = FastAPI(title="Loan Servicer", version=__version__)
    app.state.sim = sim
    app.state.metrics = metrics

    def refresh_gauges():
        bc = {b: 0 for b in BUCKETS}
        upb = 0
        for lo in sv.loans.values():
            if lo.status == "active":
                bc[bucket(lo, today)] += 1
                upb += lo.principal
        metrics.set_gauge(
            "ls_loans_by_bucket", "Active loans by delinquency bucket.", {(("bucket", b),): v for b, v in bc.items()}
        )
        metrics.set_gauge("ls_upb_cents", "Unpaid principal balance of active loans, in cents.", {(): upb})
        metrics.set_gauge(
            "ls_sim_invariant_failures",
            "Invariant failures across simulated months.",
            {(): summary["invariant_failures"]},
        )
        metrics.set_gauge(
            "ls_remittance_ties_out",
            "1 when every simulated month tied out to the cent.",
            {(): int(summary["remittance_ties_out"])},
        )

    refresh_gauges()

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        t = time.perf_counter()
        response = await call_next(request)
        dt = time.perf_counter() - t
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        metrics.observe_http(path, response.status_code, dt)
        response.headers["x-request-id"] = rid
        request.state.rid = rid
        log_event(
            log,
            "request",
            request_id=rid,
            method=request.method,
            route=path,
            status=response.status_code,
            duration_ms=round(dt * 1000, 2),
        )
        return response

    @app.exception_handler(ServicingError)
    async def servicing_error(_request: Request, exc: ServicingError):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    def get_loan(loan_id: str) -> Loan:
        lo = sv.loans.get(loan_id)
        if lo is None:
            raise HTTPException(404, f"no loan {loan_id}")
        return lo

    def check_month(month: str | None) -> str:
        months = [mo["month"] for mo in sim.months]
        m = month or months[-1]
        if m not in months and m != f"{today.year:04d}-{today.month:02d}":
            raise HTTPException(404, f"no month {m}; simulated months are {months[0]} to {months[-1]}")
        return m

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__, "loans": len(sv.loans), "as_of": today.isoformat()}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom():
        return metrics.render()

    @app.get("/api/summary")
    def get_summary():
        return {
            "servicer": SERVICER,
            "as_of": today.isoformat(),
            "summary": summary,
            "series": series(sim),
            "examples": examples,
            "pools": {pid: {"name": p.name, "rules": p.rules.to_dict()} for pid, p in sv.pools.items()},
        }

    @app.get("/api/loans")
    def list_loans(bucket_filter: str | None = Query(None, alias="bucket"), q: str | None = None, limit: int = 50):
        out = []
        for lo in sv.loans.values():
            if q and q.lower() not in lo.loan_id.lower() and q.lower() not in lo.borrower.name.lower():
                continue
            b = bucket(lo, today)
            if bucket_filter and b != bucket_filter:
                continue
            out.append({**lo.summary(), "bucket": b, "dpd": days_past_due(lo, today)})
            if len(out) >= min(limit, 500):
                break
        return out

    @app.get("/api/loans/{loan_id}")
    def get_loan_detail(loan_id: str):
        return loan_detail(sim, get_loan(loan_id), today)

    @app.post("/api/loans/{loan_id}/payments")
    def post_payment(loan_id: str, body: PaymentIn, request: Request):
        lo = get_loan(loan_id)
        on = body.on or today
        if on < today:
            raise HTTPException(422, f"payment date must be on or after {today.isoformat()}")
        if body.amount_cents <= 0:
            raise HTTPException(422, "amount_cents must be positive")
        t = sv.apply_payment(lo.loan_id, body.amount_cents, on, body.method)
        metrics.payments[body.method] += 1
        sv.record_audit(
            "payment",
            loan_id=loan_id,
            txn_id=t.txn_id,
            amount=t.amount,
            on=on.isoformat(),
            splits=t.splits,
            request_id=getattr(request.state, "rid", None),
        )
        log_event(log, "payment applied", loan_id=loan_id, txn_id=t.txn_id, amount=t.amount)
        refresh_gauges()
        return {"txn": t.to_dict(), "loan": loan_detail(sim, lo, today)}

    @app.post("/api/loans/{loan_id}/payments/{txn_id}/reverse")
    def post_reverse(loan_id: str, txn_id: str, body: ReverseIn | None = None):
        lo = get_loan(loan_id)
        body = body or ReverseIn()
        on = body.on or today
        res = sv.reverse_payment(lo.loan_id, txn_id, on, body.reason)
        metrics.reversals += 1
        sv.record_audit("reversal", loan_id=loan_id, txn_id=txn_id, reason=body.reason, on=on.isoformat(), **res)
        refresh_gauges()
        return {**res, "loan": loan_detail(sim, lo, today)}

    @app.get("/api/loans/{loan_id}/payoff")
    def get_payoff(loan_id: str, payoff_date: date | None = None):
        lo = get_loan(loan_id)
        d = payoff_date or today + timedelta(days=10)
        if d < today:
            raise HTTPException(422, f"payoff date must be on or after {today.isoformat()}")
        q = sv.payoff_quote(lo, d)
        metrics.payoff_quotes += 1
        sv.record_audit("payoff_quote", loan_id=loan_id, payoff_date=d.isoformat(), total=q["total"])
        return q

    @app.get("/api/loans/{loan_id}/escrow-analysis")
    def get_escrow(loan_id: str, spread: str | None = None):
        lo = get_loan(loan_id)
        if not lo.escrowed:
            raise HTTPException(409, f"loan {loan_id} has no escrow account")
        if spread not in (None, "", "lump") and not (spread.isdigit() and 1 <= int(spread) <= 60):
            raise HTTPException(422, "spread must be 1 to 60 months or 'lump'")
        effective = lo.next_due if lo.next_due > today else date(today.year, today.month, 1)
        sp = int(spread) if spread and spread.isdigit() else spread
        return analyze(lo, effective, sv.rules(lo), sp)

    @app.get("/api/collections")
    def get_collections(bucket_filter: str | None = Query(None, alias="bucket"), limit: int = 100):
        q = collections_queue(sv.loans.values(), today)
        counts = {b: 0 for b in BUCKETS}
        for r in q:
            counts[r["bucket"]] += 1
        if bucket_filter:
            q = [r for r in q if r["bucket"] == bucket_filter]
        return {
            "as_of": today.isoformat(),
            "total": sum(counts.values()),
            "by_bucket": counts,
            "queue": q[: min(limit, 1000)],
        }

    @app.get("/api/roll-rates")
    def get_roll_rates():
        last = sim.months[-1]["roll"]
        return {
            "series": series(sim),
            "last_month": sim.months[-1]["month"],
            "matrix": last,
            "roll_forward_bp": {b: roll_forward_bp(last, b) for b in BUCKETS[:-1]},
            "average_bp": summary["roll_rates_avg_bp"],
            "by_month": [{"month": mo["month"], "counts": mo["roll"]["counts"]} for mo in sim.months],
        }

    @app.get("/api/investor/remittance")
    def get_remittance(month: str | None = None):
        m = check_month(month)
        pools = [remittance(sv, pid, m) for pid in sv.pools]
        recs = [reconcile(sv, pid, m) for pid in sv.pools]
        return {
            "month": m,
            "months": [mo["month"] for mo in sim.months],
            "pools": pools,
            "ties_out": all(r["ties_out"] for r in recs),
        }

    @app.get("/api/investor/reconciliation")
    def get_recon(month: str | None = None):
        m = check_month(month)
        recs = [reconcile(sv, pid, m) for pid in sv.pools]
        return {"month": m, "pools": recs, "ties_out": all(r["ties_out"] for r in recs)}

    @app.get("/api/investor/delinquency")
    def get_delinquency():
        return {"as_of": today.isoformat(), "pools": delinquency_report(sv, today)}

    @app.get("/api/audit")
    def get_audit(limit: int = 100, loan_id: str | None = None):
        rows = [a for a in sv.audit if loan_id is None or a.get("loan_id") == loan_id]
        return rows[-min(limit, 1000) :][::-1]

    return app
