import json

import pytest
from fastapi.testclient import TestClient

from loanservicer.app import create_app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    audit = tmp_path_factory.mktemp("var") / "audit.jsonl"
    app = create_app(loans=300, months=6, seed=7, audit_path=str(audit))
    with TestClient(app) as c:
        c.audit_path = audit
        yield c


def current_loan(client):
    loans = client.get("/api/loans", params={"bucket": "current", "limit": 50}).json()
    return next(lo for lo in loans if lo["status"] == "active")["loan_id"]


def test_healthz_and_index(client):
    h = client.get("/healthz").json()
    assert h["status"] == "ok" and h["loans"] == 300 and h["as_of"] == "2025-05-01"
    r = client.get("/")
    assert r.status_code == 200 and "Loan Servicer" in r.text


def test_summary(client):
    s = client.get("/api/summary").json()
    assert s["summary"]["all_invariants_hold"] and s["summary"]["remittance_ties_out"]
    assert len(s["series"]["months"]) == 6 and s["examples"]
    assert set(s["pools"]) == {"TW-AG-21", "TW-PL-22", "TW-ARM-23", "TW-BAL"}


def test_loan_detail(client):
    lid = current_loan(client)
    d = client.get(f"/api/loans/{lid}").json()
    assert d["loan_id"] == lid and d["schedule"] and d["history"]
    assert d["schedule"][0]["due"] == d["next_due"]
    assert d["bucket"] == "current"
    assert client.get("/api/loans/NOPE").status_code == 404


def test_apply_payment_and_audit(client):
    lid = current_loan(client)
    d = client.get(f"/api/loans/{lid}").json()
    amt = d["amount_due"] or d["monthly_payment"]
    r = client.post(f"/api/loans/{lid}/payments", json={"amount_cents": amt})
    assert r.status_code == 200, r.text
    t = r.json()["txn"]
    assert t["amount"] == amt and sum(v for k, v in t["splits"].items() if k not in ("servicing_fee", "gfee")) == amt
    audit = client.get("/api/audit", params={"loan_id": lid}).json()
    assert audit[0]["action"] == "payment" and audit[0]["txn_id"] == t["txn_id"]
    lines = [json.loads(x) for x in client.audit_path.read_text().splitlines()]
    assert any(x.get("txn_id") == t["txn_id"] for x in lines)


def test_float_amount_rejected(client):
    lid = current_loan(client)
    assert client.post(f"/api/loans/{lid}/payments", json={"amount_cents": 100.5}).status_code == 422
    assert client.post(f"/api/loans/{lid}/payments", json={"amount_cents": "100"}).status_code == 422
    assert client.post(f"/api/loans/{lid}/payments", json={"amount_cents": -5}).status_code == 422


def test_reverse_payment(client):
    lid = current_loan(client)
    before = client.get(f"/api/loans/{lid}").json()
    t = client.post(f"/api/loans/{lid}/payments", json={"amount_cents": 25_000}).json()["txn"]
    r = client.post(f"/api/loans/{lid}/payments/{t['txn_id']}/reverse", json={"reason": "NSF"})
    assert r.status_code == 200, r.text
    after = r.json()["loan"]
    assert after["principal"] == before["principal"] and after["suspense"] == before["suspense"]
    assert after["late_fees"] == before["late_fees"] + 2_500
    again = client.post(f"/api/loans/{lid}/payments/{t['txn_id']}/reverse")
    assert again.status_code == 409


def test_payoff_quote(client):
    lid = current_loan(client)
    q = client.get(f"/api/loans/{lid}/payoff", params={"payoff_date": "2025-05-20"}).json()
    assert q["good_through"] == "2025-05-20" and q["total"] > q["principal"] > 0
    assert isinstance(q["per_diem"], int)
    assert client.get(f"/api/loans/{lid}/payoff", params={"payoff_date": "2024-01-01"}).status_code == 422


def test_escrow_analysis(client):
    s = client.get("/api/summary").json()
    lid = next(e["loan_id"] for e in s["examples"] if e["label"] == "escrow shortage spread")
    r = client.get(f"/api/loans/{lid}/escrow-analysis", params={"spread": "lump"}).json()
    assert len(r["projection"]) == 12 and r["notice"]
    assert client.get(f"/api/loans/{lid}/escrow-analysis", params={"spread": "x"}).status_code == 422


def test_collections_queue(client):
    q = client.get("/api/collections").json()
    assert q["total"] == sum(q["by_bucket"].values()) > 0
    scores = [r["score"] for r in q["queue"]]
    assert scores == sorted(scores, reverse=True)
    only60 = client.get("/api/collections", params={"bucket": "60"}).json()["queue"]
    assert all(r["bucket"] == "60" for r in only60)


def test_investor_reports(client):
    r = client.get("/api/investor/remittance").json()
    assert r["ties_out"] and len(r["pools"]) == 4 and r["month"] == "2025-04"
    for p in r["pools"]:
        assert p["remittance"] == p["actual_principal"] + p["actual_interest"] - p["servicing_fee"] - p["gfee"]
    rec = client.get("/api/investor/reconciliation", params={"month": "2024-12"}).json()
    assert rec["ties_out"]
    assert client.get("/api/investor/remittance", params={"month": "1999-01"}).status_code == 404
    dq = client.get("/api/investor/delinquency").json()
    assert sum(p["active_loans"] for p in dq["pools"].values()) > 0


def test_open_month_reconciles_after_api_payments(client):
    rec = client.get("/api/investor/reconciliation", params={"month": "2025-05"}).json()
    assert rec["ties_out"], rec


def test_roll_rates(client):
    r = client.get("/api/roll-rates").json()
    assert len(r["series"]["months"]) == 6 and "current->30" in r["average_bp"]
    assert set(r["matrix"]["counts"]) == {"current", "30", "60", "90", "120+"}


def test_metrics(client):
    m = client.get("/metrics").text
    assert "ls_http_requests_total" in m and 'ls_loans_by_bucket{bucket="current"}' in m
    assert "ls_remittance_ties_out 1" in m and "ls_payments_applied_total" in m
