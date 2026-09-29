"""Per rule: a 'fires' and a 'stays quiet' case, on JSON fixture patients, for BOTH data sources.

Scenario labels live in data/patients/hand-authored/*.json (expect_baseline / expect_context). The
VistA run uses the raw-RPC-reply twins of the same patients, so this also proves the same rule
fires identically for FHIR- and VistA-sourced patients.
"""

from __future__ import annotations

import pytest

from app.service import CdsService
from tests.conftest import TWIN, all_scenarios

SCEN = all_scenarios()
IDS = [f"{pid}:{key}" for pid, key, _, _ in SCEN]


def _ids(service: CdsService, source: str, pid: str, draft: dict, mode: str) -> set[str]:  # type: ignore[type-arg]
    return {a.rule_id for a in service.evaluate(source, pid, draft, mode).result.alerts}


@pytest.mark.parametrize(("pid", "key", "doc", "sc"), SCEN, ids=IDS)
@pytest.mark.parametrize("source", ["fhir", "vista"])
def test_scenario_matches_labels(service: CdsService, source: str, pid: str, key: str, doc: dict, sc: dict) -> None:  # type: ignore[type-arg]
    patient = pid if source == "fhir" else TWIN[pid]
    assert _ids(service, source, patient, sc["order"], "baseline") == set(sc["expect_baseline"])
    assert _ids(service, source, patient, sc["order"], "context") == set(sc["expect_context"])


RULES = [
    "metformin-low-egfr",
    "nsaid-raas-diuretic-aki",
    "apixaban-dose-reduction",
    "rivaroxaban-renal-dose",
    "raas-potassium-hyperkalemia",
    "lithium-interacting-drugs",
]


@pytest.mark.parametrize("rule_id", RULES)
def test_every_rule_has_a_fires_and_a_quiet_fixture_case(rule_id: str) -> None:
    fires = [s for _, _, _, s in SCEN if rule_id in s["expect_context"]]
    quiet = [s for _, _, _, s in SCEN if rule_id in s["expect_baseline"] and rule_id not in s["expect_context"]]
    if rule_id == "lithium-interacting-drugs":  # control rule: never suppressed by design
        assert fires and not quiet
    else:
        assert fires, f"{rule_id} needs a 'fires' fixture"
        assert quiet, f"{rule_id} needs a 'stays quiet' fixture"


def test_same_alert_text_for_fhir_and_vista(service: CdsService) -> None:
    for pid, _key, _doc, sc in SCEN:
        a = service.evaluate("fhir", pid, sc["order"], "context").result.alerts
        b = service.evaluate("vista", TWIN[pid], sc["order"], "context").result.alerts
        assert [(x.rule_id, x.summary, x.indicator, x.data_gap) for x in a] == [
            (x.rule_id, x.summary, x.indicator, x.data_gap) for x in b
        ], pid


def test_suppression_records_reason(service: CdsService) -> None:
    sc = next(s for p, k, _, s in SCEN if p == "hand-08")
    res = service.evaluate("fhir", "hand-08", sc["order"], "context").result
    assert not res.alerts
    assert res.suppressed[0].rule_id == "raas-potassium-hyperkalemia"
    assert any("potassium" in r for r in res.suppressed[0].reason)


def test_data_gap_card_is_info_and_flagged(service: CdsService) -> None:
    sc = next(s for p, k, _, s in SCEN if p == "hand-03")
    (alert,) = service.evaluate("fhir", "hand-03", sc["order"], "context").result.alerts
    assert alert.data_gap and alert.indicator == "info"


def test_metformin_critical_below_30(service: CdsService) -> None:
    ctx = service.load_context("vista", "100881")  # VEHU: creatinine 2.1 -> computed eGFR ~24
    from app.service import draft_order

    res = service.evaluate_ctx(ctx, draft_order("100881", "861007"), "context").result
    (alert,) = res.alerts
    assert alert.indicator == "critical"
    assert "computed by CKD-EPI 2021" in " ".join(alert.why)


def test_unmapped_draft_returns_no_alerts(service: CdsService) -> None:
    ctx = service.load_context("fhir", "hand-01")
    draft = {
        "resourceType": "MedicationRequest",
        "status": "draft",
        "medicationCodeableConcept": {"text": "UNOBTAINIUM 5MG"},
    }
    assert service.evaluate_ctx(ctx, draft, "context").result.alerts == ()


def test_rule_files_are_versioned_and_cite_public_sources(engine) -> None:  # type: ignore[no-untyped-def]
    assert len(engine.rules) >= 5
    for r in engine.rules:
        assert r.version.count(".") == 2
        assert r.card.source.url.startswith("https://")
        assert r.card.fire.why
