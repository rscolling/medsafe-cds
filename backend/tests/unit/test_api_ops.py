"""Operational endpoints: health/ready/metrics, rate limiting, audit auth, UI helper API, logging."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.audit.store import AuditStore
from app.config import Settings
from app.logging_setup import JsonFormatter, pseudonymize
from app.ratelimit import RateLimiter


def make(service, **kw):  # type: ignore[no-untyped-def]
    s = Settings(
        fhir_mode="fixtures",
        vista_mode="recorded",
        audit_db=":memory:",
        **{"rate_limit_per_minute": 10000, **kw},
    )
    return TestClient(create_app(s, sources=service.sources, audit=AuditStore(":memory:")))


def test_health_ready_metrics(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    assert c.get("/health").json() == {"status": "ok"}
    ready = c.get("/ready").json()
    assert ready["status"] == "ready" and ready["checks"]["rules"] == 6
    assert ready["checks"]["vista"]["mode"] == "recorded"
    c.get("/cds-services")
    text = c.get("/metrics").text
    assert 'medsafe_http_requests_total{method="GET",path="/cds-services",status="200"}' in text
    assert "medsafe_rules_loaded 6.0" in text
    assert c.get("/health").headers["X-Request-Id"]


def test_request_id_is_propagated(service) -> None:  # type: ignore[no-untyped-def]
    assert make(service).get("/health", headers={"X-Request-Id": "abc123"}).headers["X-Request-Id"] == "abc123"


def test_rate_limit_returns_429_and_exempts_health(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service, rate_limit_per_minute=3)
    codes = [c.get("/cds-services").status_code for _ in range(6)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]
    r = c.get("/cds-services")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    assert c.get("/health").status_code == 200
    assert "medsafe_rate_limited_total" in c.get("/metrics").text


def test_token_bucket_refills() -> None:
    now = [0.0]
    rl = RateLimiter(60, clock=lambda: now[0])
    assert all(rl.allow("a")[0] for _ in range(60))
    ok, retry = rl.allow("a")
    assert not ok and retry == pytest.approx(1.0)
    now[0] += 1.5
    assert rl.allow("a")[0]
    assert rl.allow("b")[0]
    assert RateLimiter(0).allow("x") == (True, 0.0)


def test_audit_endpoints_need_key_when_configured(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service, audit_api_key="k")
    assert c.get("/api/audit").status_code == 401
    assert c.get("/api/audit", headers={"X-API-Key": "k"}).status_code == 200
    assert c.get("/api/audit/summary", headers={"X-API-Key": "k"}).status_code == 200


def test_alerts_are_audited_with_pseudonymised_patient(service) -> None:  # type: ignore[no-untyped-def]
    from tests.contract.test_cds_hooks_contract import hook_body

    c = make(service)
    card = c.post("/cds-services/medsafe-order-sign", json=hook_body("hand-01", "861007")).json()["cards"][0]
    (event,) = c.get("/api/audit", params={"kind": "alert_shown"}).json()
    assert event["card_uuid"] == card["uuid"] and event["rule_version"] == "1.0.0"
    assert event["patient_pseudo"] != "hand-01" and len(event["patient_pseudo"]) == 12
    summary = c.get("/api/audit/summary").json()
    assert summary["alerts_shown"][0]["n"] == 1


def test_source_error_is_audited_and_counted(service) -> None:  # type: ignore[no-untyped-def]
    from tests.contract.test_cds_hooks_contract import hook_body

    c = make(service)
    c.post("/cds-services/medsafe-order-sign", json=hook_body("nobody", "861007", "vista"))
    assert c.get("/api/audit", params={"kind": "source_error"}).json()[0]["source"] == "vista"
    assert 'medsafe_source_errors_total{source="vista"} 1.0' in c.get("/metrics").text


def test_ui_helper_api(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    assert c.get("/api/sources").json()["fhir"]["reachable"] is True
    assert len(c.get("/api/patients", params={"source": "fhir"}).json()) >= 10
    assert c.get("/api/patients", params={"source": "x"}).status_code == 404
    assert any(d["rxcui"] == "861007" for d in c.get("/api/drugs").json())
    assert len(c.get("/api/rules").json()) == 6
    body = c.post("/api/compare", json={"source": "fhir", "patientId": "hand-08", "rxcui": "1801294"}).json()
    assert (
        len(body["baseline"]) == 1
        and body["context"] == []
        and body["suppressed"][0]["ruleId"] == "raas-potassium-hyperkalemia"
    )
    assert "Prototype, not clinical advice" in body["disclaimer"]
    assert c.post("/api/compare", json={"source": "fhir", "patientId": "zzz", "rxcui": "861007"}).status_code == 503
    assert c.post("/api/compare", json={"source": "fhir", "patientId": "hand-01", "rxcui": "0"}).status_code == 404
    assert c.post("/api/compare", json={"source": "bad", "patientId": "hand-01", "rxcui": "861007"}).status_code == 422


def test_json_log_formatter_and_pseudonym() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello %s", ("w",), None)
    rec.patient = "abc"
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "hello w" and out["patient"] == "abc" and "request_id" in out
    assert pseudonymize("p1", "s") == pseudonymize("p1", "s") != pseudonymize("p2", "s")
    try:
        raise ValueError("x")
    except ValueError:
        import sys

        rec2 = logging.LogRecord("x", logging.ERROR, "f", 1, "bad", (), sys.exc_info())
        assert "ValueError" in json.loads(JsonFormatter().format(rec2))["exc"]


def test_unhandled_error_returns_500_json(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    monkeypatch.setattr(
        c.app.state.service,
        "load_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    from tests.contract.test_cds_hooks_contract import hook_body

    r = TestClient(c.app, raise_server_exceptions=False).post(
        "/cds-services/medsafe-order-sign", json=hook_body("hand-01", "861007")
    )
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
