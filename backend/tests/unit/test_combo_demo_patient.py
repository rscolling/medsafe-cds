"""hand-11 / VistA twin 9000011: a combination tablet (LISINOPRIL-HCTZ 20-12.5) supplies both the ACEI and the
thiazide, so an ibuprofen draft completes the NSAID + ACEI/ARB + diuretic triple on BOTH data sources.

Synthetic, hand-authored patient (scripts/build_hand_authored.py). Prototype, not clinical advice.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.audit.store import AuditStore
from app.config import Settings
from app.service import CdsService, draft_order

IBUPROFEN = "197807"
RULE = "nsaid-raas-diuretic-aki"


def _client(service: CdsService) -> TestClient:
    s = Settings(fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:", rate_limit_per_minute=10000)
    return TestClient(create_app(s, sources=service.sources, audit=AuditStore(":memory:")))


def test_combo_tablet_expands_to_acei_and_thiazide_on_both_sources(service: CdsService) -> None:
    for source, pid in (("fhir", "hand-11"), ("vista", "9000011")):
        ctx = service.load_context(source, pid)
        by_name = {m.name: m for m in ctx.meds}
        assert set(by_name) == {"lisinopril", "hydrochlorothiazide"}, source
        assert by_name["lisinopril"].strength_mg == 20 and by_name["hydrochlorothiazide"].strength_mg == 12.5
        assert not ctx.partial_meds and not ctx.unmapped_meds
        egfr = ctx.latest_lab("egfr")
        assert egfr is not None and 30 <= egfr.value < 60, source  # moderately reduced


def test_ibuprofen_fires_triple_whammy_on_fhir_and_vista_with_identical_cards(service: CdsService) -> None:
    out = {}
    for source, pid in (("fhir", "hand-11"), ("vista", "9000011")):
        res = service.evaluate(source, pid, draft_order(pid, IBUPROFEN), "context").result
        assert [a.rule_id for a in res.alerts] == [RULE], source
        (alert,) = res.alerts
        assert alert.indicator == "warning" and not alert.data_gap
        # the why-lines differ only in eGFR provenance: FHIR carries a reported eGFR, VistA computes it (CKD-EPI 2021)
        why = tuple(re.sub(r" \((reported|computed[^)]*)\)", "", w) for w in alert.why)
        out[source] = (alert.rule_id, alert.summary, alert.indicator, alert.data_gap, why)
    assert out["fhir"] == out["vista"]  # parity: same rule, summary and why, apart from eGFR provenance


def test_combo_patient_is_listed_for_the_ui_on_both_sources(service: CdsService) -> None:
    c = _client(service)
    fhir = {p["id"]: p for p in c.get("/api/patients", params={"source": "fhir"}).json()}
    vista = {p["id"]: p for p in c.get("/api/patients", params={"source": "vista"}).json()}
    assert fhir["hand-11"]["kind"] == "hand-authored" and "LISINOPRIL-HCTZ" in fhir["hand-11"]["label"]
    assert vista["9000011"]["kind"] == "vehu-synthetic-overlay" and "twin of hand-11" in vista["9000011"]["label"]
    for source, pid in (("fhir", "hand-11"), ("vista", "9000011")):
        body = c.post("/api/compare", json={"source": source, "patientId": pid, "rxcui": IBUPROFEN}).json()
        assert sorted(body["patient"]["meds"]) == ["hydrochlorothiazide", "lisinopril"]
        assert [card["extension"]["org.medsafe.ruleId"] for card in body["context"]] == [RULE]
