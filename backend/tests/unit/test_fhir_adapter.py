"""FHIR adapter over HTTP tested with httpx.MockTransport (no HAPI needed) + fixture mode."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.adapters.base import SourceUnavailableError
from app.adapters.fhir.adapter import FhirAdapter, FixtureStore
from app.config import REPO_ROOT

FILES = sorted((REPO_ROOT / "data/patients/hand-authored").glob("*.json"))


def store() -> FixtureStore:
    return FixtureStore(FILES)


def bundle(resources: list[dict[str, Any]], next_url: str | None = None) -> dict[str, Any]:
    b: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


def http_adapter(handler, mode: str = "http") -> FhirAdapter:  # type: ignore[no-untyped-def]
    return FhirAdapter(
        store(),
        mode=mode,
        base_url="http://hapi/fhir",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_http_paging_and_params() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.path.endswith("/MedicationRequest") and "page2" not in str(req.url):
            return httpx.Response(200, json=bundle([{"id": "a"}], "http://hapi/fhir?page2"))
        if "page2" in str(req.url):
            return httpx.Response(200, json=bundle([{"id": "b"}]))
        return httpx.Response(200, json={"resourceType": "Patient", "id": "p1"})

    a = http_adapter(handler)
    assert [m["id"] for m in a.get_active_meds("p1")] == ["a", "b"]
    assert "status=active" in seen[0] and "patient=p1" in seen[0]
    assert a.get_patient("p1")["id"] == "p1"


def test_http_conditions_observations() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bundle([{"id": req.url.path.rsplit("/", 1)[-1]}]))

    a = http_adapter(handler)
    assert a.get_problems("p")[0]["id"] == "Condition"
    assert a.get_lab_series("p")[0]["id"] == "Observation"


def test_http_errors_become_source_unavailable() -> None:
    a = http_adapter(lambda r: httpx.Response(500))
    with pytest.raises(SourceUnavailableError):
        a.get_patient("p")
    assert a.status()["reachable"] is False

    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SourceUnavailableError):
        http_adapter(boom).get_active_meds("p")


def test_paging_failure() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if "next" in str(req.url):
            return httpx.Response(503)
        return httpx.Response(200, json=bundle([{"id": "a"}], "http://hapi/fhir?next"))

    with pytest.raises(SourceUnavailableError, match="paging"):
        http_adapter(handler).get_problems("p")


def test_auto_mode_falls_back_to_fixtures_when_hapi_down() -> None:
    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    a = http_adapter(down, mode="auto")
    assert a.mode == "fixtures"
    assert a.get_patient("hand-01")["id"] == "hand-01"
    assert a.status() == {
        "mode": "fixtures",
        "base_url": "http://hapi/fhir",
        "patients": 10,
        "requested_mode": "auto",
        "fell_back_to_fixtures": True,
        "reachable": True,
    }


def test_auto_mode_uses_hapi_when_up() -> None:
    a = http_adapter(lambda r: httpx.Response(200, json={"resourceType": "CapabilityStatement"}), mode="auto")
    assert a.mode == "http"
    assert a.status()["reachable"] is True


def test_fixture_mode_interface() -> None:
    a = FhirAdapter(store(), mode="fixtures")
    assert a.get_active_meds("hand-04")[0]["status"] == "active"
    assert len(a.get_problems("hand-04")) == 3
    assert any(o["code"]["coding"][0]["code"] == "98979-8" for o in a.get_lab_series("hand-04"))
    assert {p.synthetic_kind for p in a.list_patients()} == {"hand-authored"}
    with pytest.raises(SourceUnavailableError):
        a.get_patient("nope")


def test_hand_authored_files_are_labeled_synthetic() -> None:
    for f in FILES:
        doc = json.loads(f.read_text())
        assert doc["synthetic"] is True
        assert doc["resources"][0]["meta"]["tag"][0]["code"] == "hand-authored-synthetic"
        assert doc["scenarios"], f.name
