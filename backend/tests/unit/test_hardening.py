"""QA hardening: malformed input fails open, size limits, secure defaults, VistA patient validation, no PHI in logs."""

from __future__ import annotations

import copy
import json
import logging
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.adapters.base import SourceUnavailableError
from app.adapters.vista import parsing
from app.adapters.vista.adapter import VistaAdapter
from app.api.app import _bundle_entries, create_app
from app.audit.store import AuditStore
from app.config import DEFAULT_SALT, InsecureConfigError, Settings
from app.context import _dose_mg, build_context, drug_from_codeable, order_from_resource
from app.logging_setup import redact_error, short_hash
from tests.contract.test_cds_hooks_contract import hook_body

URL = "/cds-services/medsafe-order-sign"


def make(service, audit: AuditStore | None = None, **kw: Any) -> TestClient:  # type: ignore[no-untyped-def]
    s = Settings(
        fhir_mode="fixtures",
        vista_mode="recorded",
        audit_db=":memory:",
        **{"rate_limit_per_minute": 100000, **kw},
    )
    return TestClient(create_app(s, sources=service.sources, audit=audit or AuditStore(":memory:")))


# ------------------------------------------------------------------ 1. malformed nested types
def _mut(path: list[str | int], value: Any, patient: str = "hand-01") -> dict[str, Any]:
    body = hook_body(patient, "861007")
    node: Any = body
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return body


ORDER = ["context", "draftOrders", "entry", 0, "resource"]
MALFORMED = {
    "entry_junk": _mut(["context", "draftOrders", "entry"], [1, None]),
    "entry_not_list": _mut(["context", "draftOrders", "entry"], "x"),
    "resource_int": _mut(["context", "draftOrders", "entry"], [{"resource": 5}]),
    "draft_orders_str": _mut(["context", "draftOrders"], "x"),
    "concept_str": _mut([*ORDER, "medicationCodeableConcept"], "metformin"),
    "coding_str": _mut([*ORDER, "medicationCodeableConcept"], {"coding": "x"}),
    "coding_items_junk": _mut([*ORDER, "medicationCodeableConcept"], {"coding": [1, None, "x"], "text": 5}),
    "dosage_str": _mut([*ORDER, "dosageInstruction"], "x"),
    "dosage_junk": _mut(
        [*ORDER, "dosageInstruction"], [{"doseAndRate": "x"}, 3, {"doseAndRate": [{"doseQuantity": 4}]}]
    ),
    "dose_value_str": _mut(
        [*ORDER, "dosageInstruction"], [{"doseAndRate": [{"doseQuantity": {"unit": "mg", "value": "lots"}}]}]
    ),
    "id_dict": _mut([*ORDER, "id"], {"a": 1}),
    "prefetch_wrong_types": {
        **hook_body("hand-01", "861007"),
        "prefetch": {"patient": 5, "medications": "x", "conditions": [1], "observations": None},
    },
    "prefetch_junk_entries": {
        **hook_body("hand-01", "861007"),
        "prefetch": {
            "patient": {"resourceType": "Patient", "birthDate": 5, "gender": ["x"]},
            "medications": {"resourceType": "Bundle", "entry": [1, {"resource": "x"}, {"resource": {"status": {}}}]},
            "conditions": {"resourceType": "Bundle", "entry": [{"resource": {"code": "x", "clinicalStatus": 3}}]},
            "observations": {
                "resourceType": "Bundle",
                "entry": [{"resource": {"valueQuantity": "x", "code": 4, "effectiveDateTime": 7}}],
            },
        },
    },
    "source_not_string": _mut(["extension", "org.medsafe.source"], ["fhir"]),
    "patient_id_dict": _mut(["context", "patientId"], {"a": 1}),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_payload_never_returns_500(service, name: str) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    r = c.post(URL, json=MALFORMED[name])
    assert r.status_code in (200, 422), (name, r.status_code, r.text[:200])
    if r.status_code == 200:
        assert isinstance(r.json()["cards"], list)


def test_unusable_prefetch_fails_open_with_header_and_audit(service) -> None:  # type: ignore[no-untyped-def]
    audit = AuditStore(":memory:")
    c = make(service, audit)
    r = c.post(URL, json=MALFORMED["prefetch_wrong_types"])
    assert r.status_code == 200 and r.json() == {"cards": []}
    assert r.headers["X-Medsafe-Degraded"] == "source-unavailable"
    assert audit.query("source_error")


def test_engine_exception_fails_open_and_is_audited(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    audit = AuditStore(":memory:")
    c = make(service, audit)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("engine bug for patient hand-01")

    monkeypatch.setattr("app.service.CdsService.evaluate_ctx", boom)
    r = c.post(URL, json=hook_body("hand-01", "861007"))
    assert r.status_code == 200 and r.json() == {"cards": []}
    assert r.headers["X-Medsafe-Degraded"] == "source-unavailable"
    (row,) = audit.query("source_error")
    assert "hand-01" not in row["detail"] and "RuntimeError" in row["detail"]


def test_defensive_helpers_accept_garbage() -> None:
    assert _bundle_entries({"resourceType": "Bundle", "entry": [1, None, {"resource": 5}, {"resource": {"a": 1}}]}) == [
        {"a": 1}
    ]
    assert _bundle_entries("x") == [] and _bundle_entries({"resourceType": "Bundle", "entry": "x"}) == []
    assert _dose_mg({"dosageInstruction": "x"}) is None
    assert _dose_mg({"dosageInstruction": [{"doseAndRate": [{"doseQuantity": {"unit": "mg", "value": True}}]}]}) is None
    assert _dose_mg({"dosageInstruction": [{"doseAndRate": [{"doseQuantity": {"unit": "mg", "value": "5"}}]}]}) == 5.0
    assert drug_from_codeable("metformin").name == "unknown"  # type: ignore[arg-type]
    assert drug_from_codeable({"coding": "x"}).name == "unknown"
    assert order_from_resource({"medicationCodeableConcept": 3, "id": {"x": 1}}).order_id is None
    with pytest.raises(ValueError, match="patient resource"):
        build_context("x", [], [], [], source="t")  # type: ignore[arg-type]
    ctx = build_context({"gender": ["x"], "birthDate": 5}, [1], [1], [{"valueQuantity": "x"}], source="t")  # type: ignore[list-item]
    assert ctx.sex == "unknown" and not ctx.meds


def test_random_corruption_never_500(service) -> None:  # type: ignore[no-untyped-def]
    """Deterministic fuzz: replace every node of a valid request with junk, one at a time."""
    c = make(service)
    base = hook_body("hand-01", "861007")
    junk = [None, 5, "x", [], [1], {}, {"a": None}, True, 1.5e400]
    paths: list[list[str | int]] = []

    def walk(node: Any, path: list[str | int]) -> None:
        if path:
            paths.append(path)
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, [*path, k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, [*path, i])

    walk(base, [])
    assert len(paths) > 15
    for path in paths:
        for j in junk:
            body = copy.deepcopy(base)
            node: Any = body
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = j
            r = c.post(URL, content=json.dumps(body, allow_nan=True), headers={"content-type": "application/json"})
            assert r.status_code < 500, (path, j, r.status_code)


# ------------------------------------------------------------------ 2. size limits
def test_body_over_limit_is_413_by_content_length(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service, max_body_bytes=2000)
    r = c.post(URL, content=b"{" + b" " * 5000 + b"}", headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_body_over_limit_is_413_when_streamed_without_length(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service, max_body_bytes=2000)

    def chunks():  # type: ignore[no-untyped-def]
        yield b'{"a": "'
        for _ in range(10):
            yield b"x" * 1000
        yield b'"}'

    r = c.post(URL, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_default_limit_is_one_megabyte_and_normal_requests_pass(service) -> None:  # type: ignore[no-untyped-def]
    assert Settings().max_body_bytes == 1_000_000 and Settings().max_draft_orders == 50
    assert make(service).post(URL, json=hook_body("hand-01", "861007")).status_code == 200


def test_draft_order_cap(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)

    def with_n(n: int) -> dict[str, Any]:
        body = hook_body("hand-02", "861007")
        entry = body["context"]["draftOrders"]["entry"][0]
        body["context"]["draftOrders"]["entry"] = [copy.deepcopy(entry) for _ in range(n)]
        return body

    assert c.post(URL, json=with_n(50)).status_code == 200
    r = c.post(URL, json=with_n(51))
    assert r.status_code == 422 and "too many draft orders" in r.json()["detail"]


def test_overlong_patient_and_hook_instance_rejected(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    assert c.post(URL, json=_mut(["context", "patientId"], "9" * 200)).status_code == 422
    assert c.post(URL, json=_mut(["hookInstance"], "h" * 500)).status_code == 422


# ------------------------------------------------------------------ 3. hardened defaults
def test_default_settings_have_findings_and_dev_only_warns(caplog) -> None:  # type: ignore[no-untyped-def]
    s = Settings(audit_api_key="", pseudonym_salt=DEFAULT_SALT, env="dev")
    assert len(s.security_findings()) == 2
    lg = logging.getLogger("t-harden")
    with caplog.at_level(logging.WARNING, logger="t-harden"):
        s.enforce_security(lg)
    assert "INSECURE DEV DEFAULT" in caplog.text and "do not expose" in caplog.text


def test_production_refuses_insecure_defaults(service) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InsecureConfigError, match="MEDSAFE_AUDIT_API_KEY"):
        make(service, env="production")
    with pytest.raises(InsecureConfigError, match="MEDSAFE_PSEUDONYM_SALT"):
        make(service, env="production", audit_api_key="k" * 20)
    with pytest.raises(InsecureConfigError):
        make(service, env="production", audit_api_key="k", pseudonym_salt="short")
    make(service, env="production", audit_api_key="k" * 20, pseudonym_salt="s" * 24)  # hardened: starts


def test_hardened_settings_have_no_findings() -> None:
    assert Settings(audit_api_key="key", pseudonym_salt="a-long-random-salt").security_findings() == []


def test_key_protects_metrics_and_switches_docs_off(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service, audit_api_key="k")
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"X-API-Key": "k"}).status_code == 200
    assert c.get("/metrics", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    assert c.get("/health").status_code == 200  # liveness probes stay open


def test_keyless_dev_serves_docs_and_metrics(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    assert c.get("/docs").status_code == 200 and c.get("/metrics").status_code == 200


def test_cors_allows_only_needed_headers(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service, cors_origins=("http://localhost:5173",))
    ok = c.options(
        URL,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        },
    )
    assert ok.status_code == 200
    bad = c.options(
        URL,
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-evil",
        },
    )
    assert bad.status_code == 400
    assert "*" not in ok.headers.get("access-control-allow-headers", "")


# ------------------------------------------------------------------ 5. VistA patient validation
class _FakeClient:
    def __init__(self, select_reply: str) -> None:
        self.select_reply, self.calls = select_reply, []

    def call(self, rpc: str, *params: str) -> str:
        self.calls.append(rpc)
        return self.select_reply if rpc == "ORWPT SELECT" else ""

    def close(self) -> None:
        return None


def _adapter(reply: str) -> tuple[VistaAdapter, _FakeClient]:
    fake = _FakeClient(reply)
    return VistaAdapter(fake, mode="live", demo_patients=[]), fake


@pytest.mark.parametrize("bad", ["abc", "1; DROP", "12 34", "", "-1", "9" * 13, "１２３"])
def test_vista_rejects_non_numeric_dfn_before_any_rpc(bad: str) -> None:
    a, fake = _adapter("X^M^2380420")
    with pytest.raises(SourceUnavailableError, match="invalid VistA patient id"):
        a.get_patient(bad)
    assert fake.calls == []


@pytest.mark.parametrize("reply", ["-1^^^^^Patient is unknown to CPRS.", "", "\r\n", "^M^2380420"])
def test_vista_nonexistent_patient_is_source_unavailable_not_no_data(reply: str) -> None:
    a, _ = _adapter(reply)
    with pytest.raises(SourceUnavailableError):
        a.get_patient("99999999")
    with pytest.raises(ValueError, match="not found"):
        parsing.parse_patient_select("99999999", reply)


@pytest.mark.parametrize("pid", ["abc", "99999999", "0"])
def test_vista_nonexistent_patient_via_api_fails_open_without_data_gap_card(service, pid: str) -> None:  # type: ignore[no-untyped-def]
    audit = AuditStore(":memory:")
    c = make(service, audit)
    r = c.post(URL, json=hook_body(pid, "861007", "vista"))
    assert r.status_code == 200 and r.json() == {"cards": []}
    assert r.headers["X-Medsafe-Degraded"] == "source-unavailable"
    assert audit.query("source_error") and not audit.query("alert_shown")


# ------------------------------------------------------------------ 6. no ids / order text in logs and audit
SECRET_ID = "8675309123"
SECRET_ORDER = "SECRETDRUGNAME-XYZZY 500MG"


def test_redact_error_helpers() -> None:
    out = redact_error(KeyError("unknown patient 8675309123 / abc-patient"), "abc-patient")
    assert "8675309123" not in out and "abc-patient" not in out and out.startswith("KeyError:")
    assert len(redact_error(ValueError("x" * 1000))) <= 200
    assert short_hash("a") != short_hash("b") and len(short_hash("a")) == 10


@pytest.mark.parametrize("source", ["fhir", "vista"])
def test_patient_id_never_reaches_logs_or_audit_on_error(service, capsys, source: str) -> None:  # type: ignore[no-untyped-def]
    audit = AuditStore(":memory:")
    c = make(service, audit)
    r = c.post(URL, json=hook_body(SECRET_ID, "861007", source))
    assert r.headers["X-Medsafe-Degraded"] == "source-unavailable"
    logs = capsys.readouterr().out
    assert "evaluation failed; returning no cards" in logs  # the log line exists (test is not vacuous)
    assert SECRET_ID not in logs
    assert SECRET_ID not in json.dumps(audit.query(None, None, 100))


def test_compare_endpoint_error_hides_patient_id(service, capsys) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    r = c.post("/api/compare", json={"source": "fhir", "patientId": SECRET_ID, "rxcui": "861007"})
    logs = capsys.readouterr().out
    assert r.status_code == 503 and SECRET_ID not in r.text
    assert "compare: source unavailable" in logs and SECRET_ID not in logs


def test_unmapped_order_text_is_not_logged(service, capsys) -> None:  # type: ignore[no-untyped-def]
    body = _mut([*ORDER, "medicationCodeableConcept"], {"text": SECRET_ORDER})
    c = make(service)
    r = c.post(URL, json=body)
    logs = capsys.readouterr().out
    (card,) = r.json()["cards"]  # an unidentified draft is an explicit info card, not a silent empty response
    assert card["indicator"] == "info" and card["extension"]["org.medsafe.unchecked"] is True
    assert "SECRETDRUGNAME" not in json.dumps(card)
    assert "could not be checked" in logs and "order_fingerprint" in logs  # present, so the next line means something
    assert "SECRETDRUGNAME" not in logs and "XYZZY" not in logs


# ------------------------------------------------------------------ nits
def test_422_does_not_echo_request_body(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    body = hook_body("hand-01", "861007")
    body["hookInstance"] = 12345
    body["context"] = {"patientId": SECRET_ID, "note": "PHI-LEAK-CANARY"}
    body["prefetch"] = "PHI-LEAK-CANARY"
    r = c.post(URL, json=body)
    assert r.status_code == 422
    assert "PHI-LEAK-CANARY" not in r.text and "input" not in json.dumps(r.json())
    assert all({"loc", "msg", "type"} == set(e) for e in r.json()["detail"])


def test_trailing_slash_is_served_without_redirect(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    r = c.get("/cds-services/", follow_redirects=False)
    assert r.status_code == 200 and r.json()["services"]
    r2 = c.post(f"{URL}/", json=hook_body("hand-01", "861007"), follow_redirects=False)
    assert r2.status_code == 200 and r2.json()["cards"]


def test_metrics_use_route_templates_not_raw_paths(service) -> None:  # type: ignore[no-untyped-def]
    c = make(service)
    c.get(f"/nope/{uuid.uuid4()}")
    text = c.get("/metrics").text
    assert 'path="unmatched"' in text and "/nope/" not in text


def test_override_reason_system_is_a_urn() -> None:
    from app.api.cards import OVERRIDE_SYSTEM

    assert OVERRIDE_SYSTEM.startswith("urn:")


def test_vendored_broker_has_no_builtin_credentials(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from vista_clients.rpc.broker import VistABroker
    from vista_clients.rpc.errors import AuthenticationError

    monkeypatch.delenv("VISTA_ACCESS_CODE", raising=False)
    monkeypatch.delenv("VISTA_VERIFY_CODE", raising=False)
    b = VistABroker("localhost", 9430)
    with pytest.raises(AuthenticationError, match="no VistA credentials"):
        b._resolve_credentials(None, None)
    monkeypatch.setenv("VISTA_ACCESS_CODE", "a")
    monkeypatch.setenv("VISTA_VERIFY_CODE", "v")
    assert b._resolve_credentials(None, None)[:2] == ("a", "v")
    assert b._resolve_credentials("x", "y")[:2] == ("x", "y")


def test_fhir_auto_fallback_is_reported_in_status(service) -> None:  # type: ignore[no-untyped-def]
    st = service.sources["fhir"].status()
    assert st["requested_mode"] == "fixtures" and st["fell_back_to_fixtures"] is False
