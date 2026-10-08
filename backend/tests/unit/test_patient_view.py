"""Patient picker identity + the active-medications popup: view helpers, adapters and the read-only API."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.base import PatientSummary, SourceUnavailableError
from app.adapters.fhir.adapter import FhirAdapter, FixtureStore
from app.adapters.vista.adapter import VistaAdapter
from app.adapters.vista.rpc import VistaUnavailableError
from app.api.app import create_app
from app.audit.store import AuditStore
from app.config import REPO_ROOT, Settings
from app.logging_setup import pseudonymize
from app.mapping.drugs import default_mapper
from app.patient_view import age_on, medication_summary, patient_identity

TODAY = date(2026, 10, 8)
RX = "http://www.nlm.nih.gov/research/umls/rxnorm"
IDENTITY_KEYS = ("name", "sex", "birthDate", "age", "mrn")


def client(service: Any, **kw: Any) -> TestClient:
    s = Settings(
        fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:", **{"rate_limit_per_minute": 10000, **kw}
    )
    return TestClient(create_app(s, sources=service.sources, audit=AuditStore(":memory:"), today=lambda: TODAY))


# ------------------------------------------------------------------ identity
def test_identity_from_text_name_sex_dob_age() -> None:
    p = {"resourceType": "Patient", "name": [{"text": "Synthetic, A"}], "gender": "female", "birthDate": "1952-03-15"}
    assert patient_identity(p, TODAY) == {
        "name": "Synthetic, A",
        "sex": "F",
        "birthDate": "1952-03-15",
        "age": 74,
        "mrn": None,
        "demographics": "ok",
    }


def test_identity_family_given_mrn_and_odd_values() -> None:
    p = {
        "name": [{"family": "Doe", "given": ["Jo", "Q", 7]}],
        "gender": 3,
        "birthDate": "1960",
        "identifier": [
            {"type": {"coding": [{"system": "urn:other", "code": "MR"}]}, "value": "nope"},
            {"type": {"coding": [{"code": "SS"}]}, "value": "000"},
            {
                "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]},
                "value": "",
            },
            {
                "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}]},
                "value": "MRN-1",
            },
        ],
    }
    out = patient_identity(p, TODAY)
    assert out["name"] == "Doe, Jo Q" and out["sex"] is None and out["age"] is None and out["mrn"] == "MRN-1"
    assert patient_identity({"name": [{"text": "  "}, {"given": ["Solo"]}], "gender": "other"}, TODAY)["name"] == "Solo"
    assert patient_identity({"name": "x", "gender": "weird"}, TODAY)["name"] is None
    assert patient_identity({"gender": "weird"}, TODAY)["sex"] == "U"


def test_identity_unavailable_and_age_edges() -> None:
    assert patient_identity(None, TODAY) == {
        "name": None,
        "sex": None,
        "birthDate": None,
        "age": None,
        "mrn": None,
        "demographics": "unavailable",
    }
    assert age_on("1952-10-08", TODAY) == 74 and age_on("1952-10-09", TODAY) == 73
    assert age_on("2030-01-01", TODAY) is None and age_on("1952-13-45", TODAY) is None and age_on(None, TODAY) is None


# ------------------------------------------------------------------ medication summaries
def test_coded_fhir_med_with_dose() -> None:
    med = {
        "id": "m1",
        "status": "active",
        "medicationCodeableConcept": {
            "coding": [{"system": RX, "code": "314076", "display": "lisinopril 10 MG Oral Tablet"}]
        },
        "dosageInstruction": [
            {"doseAndRate": [{"doseQuantity": {"value": 10.0, "unit": "mg"}}]},
            {"doseAndRate": [{"doseQuantity": {"value": 2}}]},
        ],
    }
    out = medication_summary(med)
    assert out["name"] == "lisinopril 10 MG Oral Tablet" and out["sig"] == "10 mg; 2"
    assert (
        out["rxnorm"] == [{"code": "314076", "display": "lisinopril 10 MG Oral Tablet"}]
        and out["rxnormSource"] == "coded"
    )
    assert out["status"] == "active" and out["sourceStatus"] is None and out["category"] is None


def test_free_text_vista_med_is_mapped_and_keeps_source_status() -> None:
    med = {
        "id": 5,
        "medicationCodeableConcept": {"text": "LISINOPRIL-HCTZ 20-12.5 TAB"},
        "dosageInstruction": [{"text": "Sig: TAKE ONE TABLET BY MOUTH DAILY"}],
        "category": [{"text": ""}, {"text": "outpatient"}],
        "extension": [{"url": "urn:medsafe:vista-order-status", "valueString": "PENDING"}, "junk"],
    }
    out = medication_summary(med, default_mapper())
    assert out["id"] is None and out["status"] == "unknown" and out["sourceStatus"] == "PENDING"
    assert out["sig"] == "TAKE ONE TABLET BY MOUTH DAILY" and out["category"] == "outpatient"
    assert {r["display"] for r in out["rxnorm"]} == {"lisinopril", "hydrochlorothiazide"} and out[
        "rxnormSource"
    ] == "mapped"


def test_unrecognised_and_nameless_meds() -> None:
    out = medication_summary({"medicationCodeableConcept": {"text": "GINKGO TAB"}})
    assert out["rxnorm"] == [] and out["rxnormSource"] is None and out["sig"] is None
    assert medication_summary({})["name"] == "unnamed medication"


# ------------------------------------------------------------------ adapters
def test_fhir_list_carries_patient_resources(fhir) -> None:  # type: ignore[no-untyped-def]
    rows = fhir.list_patients()
    assert rows and all(isinstance(r, PatientSummary) and r.patient and r.patient["id"] == r.id for r in rows)


def _http_fhir(handler: Any) -> FhirAdapter:
    files = sorted((REPO_ROOT / "data/patients/hand-authored").glob("*.json"))
    return FhirAdapter(
        FixtureStore(files),
        mode="http",
        base_url="http://hapi/fhir",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        demo_ids=["hand-01", "hand-02", "missing"],
    )


def test_fhir_http_list_reads_demographics_from_hapi_in_one_search() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        entry = [{"resource": {"resourceType": "Patient", "id": "hand-01", "name": [{"text": "From HAPI"}]}}]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": entry})

    rows = _http_fhir(handler).list_patients()
    assert [r.id for r in rows] == ["hand-01", "hand-02"]
    assert rows[0].patient is not None and rows[0].patient["name"][0]["text"] == "From HAPI"
    assert rows[1].patient is not None and rows[1].patient["name"][0]["text"] == "Synthetic, B"  # bundled copy
    assert len(seen) == 1 and seen[0].url.params["_id"] == "hand-01,hand-02"


def test_fhir_http_list_falls_back_to_bundled_demographics(caplog: pytest.LogCaptureFixture) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with caplog.at_level("WARNING", logger="medsafe.fhir"):
        rows = _http_fhir(handler).list_patients()
    assert rows[0].patient is not None and rows[0].patient["name"][0]["text"] == "Synthetic, A"
    assert "bundled demographics" in caplog.text
    assert (
        FhirAdapter(
            FixtureStore([]), mode="http", client=httpx.Client(transport=httpx.MockTransport(handler)), demo_ids=[]
        ).list_patients()
        == []
    )


class _Broker:
    """Fake live broker: ORWPT SELECT replies by DFN; ``down`` raises like an open circuit."""

    def __init__(self, replies: dict[str, str], down: bool = False) -> None:
        self.replies, self.down, self.calls = replies, down, 0

    def call(self, rpc: str, *params: str) -> str:
        self.calls += 1
        if self.down:
            raise VistaUnavailableError("circuit open")
        return self.replies[params[0]]

    def close(self) -> None:
        return None


def _demo(*dfns: str) -> list[dict[str, str]]:
    return [{"dfn": d, "label": f"VEHU: scenario {d}"} for d in dfns]


def test_vista_list_uses_orwpt_select_only_and_caches() -> None:
    broker = _Broker({"1": "ONE,PATIENT^M^2350407^000000000", "2": "-1^^^^^Patient is unknown to CPRS."})
    a = VistaAdapter(broker, mode="live", demo_patients=_demo("1", "2"))
    rows = {r.id: r for r in a.list_patients()}
    assert rows["1"].patient is not None and rows["1"].patient["name"][0]["text"] == "ONE,PATIENT"
    assert rows["2"].patient is None  # unknown DFN: listed by DFN, never a guessed name
    calls = broker.calls
    a.list_patients()
    assert broker.calls == calls + 1  # only the failed one is retried; the known one is cached


def test_vista_list_stops_calling_a_down_broker(caplog: pytest.LogCaptureFixture) -> None:
    broker = _Broker({}, down=True)
    a = VistaAdapter(broker, mode="live", demo_patients=_demo("1", "2", "3"))
    with caplog.at_level("WARNING", logger="medsafe.vista"):
        rows = a.list_patients()
    assert [r.patient for r in rows] == [None, None, None] and broker.calls == 1
    assert "listed by DFN only" in caplog.text


def test_vista_list_reuses_a_cached_chart(vista) -> None:  # type: ignore[no-untyped-def]
    vista.get_patient("100881")
    row = next(r for r in vista.list_patients() if r.id == "100881")
    assert row.patient is not None and row.patient["identifier"][0]["value"] == "100881"


def test_recorded_vista_every_listed_patient_has_demographics(vista) -> None:  # type: ignore[no-untyped-def]
    missing = [r.id for r in vista.list_patients() if r.patient is None]
    assert missing == []


# ------------------------------------------------------------------ API
def test_patients_endpoint_returns_identity_without_clinical_terms(service) -> None:  # type: ignore[no-untyped-def]
    c = client(service)
    drug_words = {i.name.split()[0].lower() for i in default_mapper().all_ingredients()} | {
        "egfr",
        "nsaid",
        "acei",
        "kcl",
    }
    for source in ("fhir", "vista"):
        rows = c.get("/api/patients", params={"source": source}).json()
        assert rows and all(r["demographics"] == "ok" and r["name"] and r["sex"] in {"M", "F"} for r in rows)
        for r in rows:
            shown = " ".join(str(r[k]) for k in IDENTITY_KEYS).lower()
            assert not any(w in shown for w in drug_words), (source, r["id"], shown)
    fhir = {r["id"]: r for r in c.get("/api/patients", params={"source": "fhir"}).json()}
    assert fhir["hand-01"]["name"] == "Synthetic, A" and fhir["hand-01"]["age"] == 74 and fhir["hand-01"]["sex"] == "F"
    vista = {r["id"]: r for r in c.get("/api/patients", params={"source": "vista"}).json()}
    assert vista["9000001"]["name"] == "SYNTHETICPATIENT,ATWIN"
    assert vista["100881"]["name"] == "HYPERTENSION,PATIENT FEMALE" and vista["100881"]["birthDate"] == "1938-04-20"


def test_medications_endpoint_fhir_vista_and_empty(service) -> None:  # type: ignore[no-untyped-def]
    c = client(service)
    body = c.get("/api/patients/hand-04/medications", params={"source": "fhir"}).json()
    assert body["patientId"] == "hand-04" and "Prototype, not clinical advice" in body["disclaimer"]
    assert [m["rxnorm"][0]["code"] for m in body["medications"]] == ["314076", "310429"]
    twin = c.get("/api/patients/9000004/medications", params={"source": "vista"}).json()["medications"]
    assert [m["name"] for m in twin] == ["LISINOPRIL 10MG TAB", "FUROSEMIDE 20MG TAB"]
    assert all(m["sourceStatus"] == "PENDING" and m["rxnormSource"] == "mapped" for m in twin)
    assert c.get("/api/patients/100881/medications", params={"source": "vista"}).json()["medications"] == []


def test_medications_endpoint_errors(service) -> None:  # type: ignore[no-untyped-def]
    c = client(service)
    assert c.get("/api/patients/hand-01/medications", params={"source": "nope"}).status_code == 404
    assert c.get("/api/patients/hand-01/medications").status_code == 422
    assert c.get(f"/api/patients/{'x' * 129}/medications", params={"source": "fhir"}).status_code == 422
    r = c.get("/api/patients/nobody/medications", params={"source": "fhir"})
    assert r.status_code == 503 and r.json() == {"detail": "data source unavailable"}
    assert 'medsafe_source_errors_total{source="fhir"}' in c.get("/metrics").text


class _Raising:
    name = "fhir"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def get_active_meds(self, patient_id: str) -> list[dict[str, Any]]:
        raise self.exc

    def status(self) -> dict[str, Any]:
        return {"mode": "test", "reachable": True}


def test_medications_unknown_patient_is_404() -> None:
    s = Settings(fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:")
    c = TestClient(create_app(s, sources={"fhir": _Raising(KeyError("x"))}, audit=AuditStore(":memory:")))  # type: ignore[dict-item]
    assert c.get("/api/patients/1/medications", params={"source": "fhir"}).status_code == 404
    c2 = TestClient(
        create_app(
            s, sources={"fhir": _Raising(SourceUnavailableError("DFN 1234567 down"))}, audit=AuditStore(":memory:")
        )
    )  # type: ignore[dict-item]
    assert c2.get("/api/patients/1234567/medications", params={"source": "fhir"}).status_code == 503


def test_medications_endpoint_key_rate_limit_and_pseudonymised_log(service) -> None:  # type: ignore[no-untyped-def]
    keyed = client(service, audit_api_key="k" * 20)
    assert keyed.get("/api/patients/hand-04/medications", params={"source": "fhir"}).status_code == 401
    ok = keyed.get("/api/patients/hand-04/medications", params={"source": "fhir"}, headers={"X-API-Key": "k" * 20})
    assert ok.status_code == 200

    c = client(service, rate_limit_per_minute=2)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    api_logger = logging.getLogger("medsafe.api")
    api_logger.addHandler(handler)
    level = api_logger.level
    api_logger.setLevel(logging.INFO)
    try:
        codes = [c.get("/api/patients/hand-04/medications", params={"source": "fhir"}).status_code for _ in range(4)]
    finally:
        api_logger.removeHandler(handler)
        api_logger.setLevel(level)
    assert codes[:2] == [200, 200] and 429 in codes[2:]
    listed = [r for r in records if r.getMessage() == "medications listed"]
    assert listed and all(r.__dict__["patient"] == pseudonymize("hand-04", Settings().pseudonym_salt) for r in listed)
    assert all("hand-04" not in str(r.__dict__) for r in listed)
