"""Contract tests against the CDS Hooks 2.0 spec (https://cds-hooks.hl7.org/2.0/).

Validates: discovery shape, request handling (hook / hookInstance / context / prefetch), card
attribute rules (required fields, allowed enums, <140 char summary, no null/empty values, Markdown
detail, suggestion/selectionBehavior coupling, overrideReasons displays), feedback endpoint, and
the project's own requirement that every card carries the disclaimer, a why, and a public source.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.cards import alert_to_card
from app.audit.store import AuditStore
from app.config import Settings
from app.service import draft_order

DISCLAIMER = "Prototype, not clinical advice"


@pytest.fixture
def client(service, settings):  # type: ignore[no-untyped-def]
    app = create_app(
        Settings(
            fhir_mode="fixtures",
            vista_mode="recorded",
            audit_db=":memory:",
            rate_limit_per_minute=10000,
        ),
        sources=service.sources,
        audit=AuditStore(":memory:"),
    )
    return TestClient(app)


def hook_body(patient: str, rxcui: str, source: str = "fhir", extra: dict | None = None) -> dict[str, Any]:  # type: ignore[type-arg]
    return {
        "hook": "order-sign",
        "hookInstance": str(uuid.uuid4()),
        "context": {
            "userId": "Practitioner/demo",
            "patientId": patient,
            "draftOrders": {
                "resourceType": "Bundle",
                "type": "collection",
                "entry": [{"resource": draft_order(patient, rxcui)}],
            },
        },
        "extension": {"org.medsafe.source": source},
        **(extra or {}),
    }


def no_null_or_empty(node: Any, path: str = "$") -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            assert v is not None, f"null at {path}.{k}"
            no_null_or_empty(v, f"{path}.{k}")
    elif isinstance(node, list):
        assert node, f"empty array at {path}"
        for i, v in enumerate(node):
            no_null_or_empty(v, f"{path}[{i}]")
    elif isinstance(node, str):
        assert node != "", f"empty string at {path}"


def assert_valid_card(card: dict[str, Any]) -> None:
    assert isinstance(card["summary"], str) and 0 < len(card["summary"]) < 140
    assert card["indicator"] in {"info", "warning", "critical"}
    assert isinstance(card["source"]["label"], str) and card["source"]["label"]
    assert card["source"]["url"].startswith("https://")
    uuid.UUID(card["uuid"])
    allowed = {
        "uuid",
        "summary",
        "detail",
        "indicator",
        "source",
        "suggestions",
        "selectionBehavior",
        "overrideReasons",
        "links",
        "extension",
    }
    assert set(card) <= allowed
    if "suggestions" in card:
        assert card["selectionBehavior"] in {"at-most-one", "any"}
        for s in card["suggestions"]:
            assert s["label"]
            for a in s.get("actions", []):
                assert a["type"] in {"create", "update", "delete"} and a["description"]
                if a["type"] == "delete":
                    assert a["resourceId"].startswith("MedicationRequest/")
                else:
                    assert a["resource"]["resourceType"] == "MedicationRequest"
    for r in card.get("overrideReasons", []):
        assert r["code"] and r["system"] and r["display"]
    for link in card.get("links", []):
        assert link["type"] in {"absolute", "smart"} and link["url"].startswith("https://") and link["label"]
    no_null_or_empty(card)
    # project requirements: disclaimer, why, public source on every card
    assert DISCLAIMER in card["detail"]
    assert card["extension"]["org.medsafe.disclaimer"].startswith(DISCLAIMER)
    assert card["extension"]["org.medsafe.why"], "every alert must explain why it fired"
    assert "Why this fired" in card["detail"] and "Source:" in card["detail"]


def test_discovery(client: TestClient) -> None:
    r = client.get("/cds-services")
    assert r.status_code == 200
    services = r.json()["services"]
    assert {s["id"] for s in services} == {"medsafe-order-sign", "medsafe-order-sign-baseline"}
    for s in services:
        assert s["hook"] == "order-sign" and s["description"] and s["id"]
        assert isinstance(s["prefetch"], dict) and all("{{context.patientId}}" in t for t in s["prefetch"].values())
        assert DISCLAIMER in s["description"]
    no_null_or_empty(r.json())


@pytest.mark.parametrize(("source", "patient"), [("fhir", "hand-01"), ("vista", "9000001")])
def test_order_sign_returns_valid_cards(client: TestClient, source: str, patient: str) -> None:
    r = client.post("/cds-services/medsafe-order-sign", json=hook_body(patient, "861007", source))
    assert r.status_code == 200
    cards = r.json()["cards"]
    assert len(cards) == 1
    assert_valid_card(cards[0])
    assert cards[0]["extension"]["org.medsafe.ruleId"] == "metformin-low-egfr"
    assert any(s["actions"][0]["type"] == "delete" for s in cards[0]["suggestions"])


def test_quiet_case_returns_empty_cards_array(client: TestClient) -> None:
    r = client.post("/cds-services/medsafe-order-sign", json=hook_body("hand-02", "861007"))
    assert r.status_code == 200 and r.json() == {"cards": []}


def test_baseline_service_fires_where_context_is_quiet(client: TestClient) -> None:
    body = hook_body("hand-02", "861007")
    assert client.post("/cds-services/medsafe-order-sign-baseline", json=body).json()["cards"]
    assert client.post("/cds-services/medsafe-order-sign", json=body).json()["cards"] == []


def test_dose_update_suggestion_carries_full_resource(client: TestClient) -> None:
    r = client.post("/cds-services/medsafe-order-sign", json=hook_body("hand-06", "1364445"))
    (card,) = r.json()["cards"]
    assert_valid_card(card)
    action = card["suggestions"][0]["actions"][0]
    assert action["type"] == "update"
    assert action["resource"]["dosageInstruction"][0]["doseAndRate"][0]["doseQuantity"]["value"] == 2.5


def test_prefetch_only_request_without_backend_lookup(client: TestClient, service) -> None:  # type: ignore[no-untyped-def]
    src = service.sources["fhir"]
    pf = {
        "patient": src.get_patient("hand-01"),
        "medications": {
            "resourceType": "Bundle",
            "entry": [{"resource": r} for r in src.get_active_meds("hand-01")],
        },
        "conditions": {
            "resourceType": "Bundle",
            "entry": [{"resource": r} for r in src.get_problems("hand-01")],
        },
        "observations": {
            "resourceType": "Bundle",
            "entry": [{"resource": r} for r in src.get_lab_series("hand-01")],
        },
    }
    r = client.post(
        "/cds-services/medsafe-order-sign",
        json=hook_body("does-not-exist", "861007", extra={"prefetch": pf}),
    )
    assert r.status_code == 200 and len(r.json()["cards"]) == 1


@pytest.mark.parametrize(
    ("mutate", "status"),
    [
        (lambda b: b.update(hook="patient-view"), 400),
        (lambda b: b["context"].pop("patientId"), 422),
        (lambda b: b["context"].pop("draftOrders"), 422),
        (lambda b: b.pop("hookInstance"), 422),
        (lambda b: b.update(hookInstance="short"), 422),
        (lambda b: b["extension"].update({"org.medsafe.source": "nope"}), 422),
    ],
)
def test_invalid_requests(client: TestClient, mutate, status: int) -> None:  # type: ignore[no-untyped-def]
    body = hook_body("hand-01", "861007")
    mutate(body)
    assert client.post("/cds-services/medsafe-order-sign", json=body).status_code == status


def test_unknown_service_404(client: TestClient) -> None:
    assert client.post("/cds-services/nope", json=hook_body("hand-01", "861007")).status_code == 404


def test_fail_open_when_source_unavailable(client: TestClient) -> None:
    r = client.post("/cds-services/medsafe-order-sign", json=hook_body("no-such-patient", "861007"))
    assert r.status_code == 200 and r.json() == {"cards": []}
    assert r.headers["X-Medsafe-Degraded"] == "source-unavailable"


def test_feedback_endpoint_records_override(client: TestClient) -> None:
    card = client.post("/cds-services/medsafe-order-sign", json=hook_body("hand-01", "861007")).json()["cards"][0]
    fb = {
        "feedback": [
            {
                "card": card["uuid"],
                "outcome": "overridden",
                "outcomeTimestamp": "2026-09-29T15:00:00Z",
                "overrideReason": {
                    "reason": {
                        "code": "benefit-outweighs-risk",
                        "system": card["overrideReasons"][0]["system"],
                    },
                    "userComment": "patient MRN 12345 says fine",
                },
            }
        ]
    }
    assert client.post("/cds-services/medsafe-order-sign/feedback", json=fb).status_code == 200
    events = client.get("/api/audit", params={"kind": "feedback"}).json()
    assert events[0]["override_code"] == "benefit-outweighs-risk"
    assert events[0]["rule_id"] == "metformin-low-egfr"
    assert "12345" not in str(events), "free-text override comments must never be persisted"
    assert events[0]["comment_len"] == len("patient MRN 12345 says fine")


def test_feedback_validation(client: TestClient) -> None:
    assert client.post("/cds-services/medsafe-order-sign/feedback", json={"feedback": []}).status_code == 422
    bad = {"feedback": [{"card": "x", "outcome": "maybe", "outcomeTimestamp": "t"}]}
    assert client.post("/cds-services/medsafe-order-sign/feedback", json=bad).status_code == 422
    assert (
        client.post(
            "/cds-services/nope/feedback",
            json={"feedback": [{"card": "x", "outcome": "accepted", "outcomeTimestamp": "t"}]},
        ).status_code
        == 404
    )


def test_alert_to_card_without_order_has_no_actions(service) -> None:  # type: ignore[no-untyped-def]
    res = service.evaluate("fhir", "hand-01", draft_order("hand-01", "861007"), "context")
    card = alert_to_card(res.result.alerts[0], None)
    assert all("actions" not in s for s in card["suggestions"])
    assert_valid_card(card)


def test_every_rule_card_in_every_mode_is_spec_valid(service) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import all_scenarios

    seen = set()
    for pid, _k, _d, sc in all_scenarios():
        for mode in ("baseline", "context"):
            for a in service.evaluate("fhir", pid, sc["order"], mode).result.alerts:
                assert_valid_card(alert_to_card(a, sc["order"]))
                seen.add((a.rule_id, mode))
    assert {r for r, _ in seen} == {r.id for r in service.engine.rules}
