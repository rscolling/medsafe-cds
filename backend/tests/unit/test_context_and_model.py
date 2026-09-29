from __future__ import annotations

from datetime import date

import pytest

from app.context import build_context, drug_from_codeable, drugs_from_codeable, parse_date
from app.model import LabValue, PatientContext


def _obs(code: str, value: float, unit: str, when: str) -> dict:  # type: ignore[type-arg]
    return {
        "resourceType": "Observation",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit},
    }


def test_parse_date_variants() -> None:
    assert parse_date("2026-01-02T10:00:00Z") == date(2026, 1, 2)
    assert parse_date("2026-01-02") == date(2026, 1, 2)
    assert parse_date("2026-01") is None
    assert parse_date(None) is None


def test_anchored_as_of_uses_newest_observation() -> None:
    pt = {"id": "1", "gender": "male", "birthDate": "1960-01-01"}
    ctx = build_context(pt, [], [], [_obs("2160-0", 1.0, "mg/dL", "2012-05-01")], source="t")
    assert ctx.as_of == date(2012, 5, 1)
    assert ctx.latest_lab("egfr", 90).source == "computed"  # type: ignore[union-attr]


def test_today_policy_uses_wall_clock() -> None:
    pt = {"id": "1", "gender": "male", "birthDate": "1960-01-01"}
    ctx = build_context(pt, [], [], [_obs("2160-0", 1.0, "mg/dL", "2012-05-01")], source="t", as_of_policy="today")
    assert ctx.as_of > date(2020, 1, 1)
    assert ctx.latest_lab("egfr", 90) is None  # 2012 lab is outside a relative window from today


def test_reported_egfr_wins_over_computed() -> None:
    pt = {"id": "1", "gender": "male", "birthDate": "1960-01-01"}
    obs = [
        _obs("2160-0", 1.0, "mg/dL", "2020-01-01"),
        _obs("98979-8", 55.0, "mL/min/1.73m2", "2020-01-01"),
    ]
    ctx = build_context(pt, [], [], obs, source="t")
    assert ctx.latest_lab("egfr").value == 55.0  # type: ignore[union-attr]
    assert ctx.latest_lab("egfr").source == "reported"  # type: ignore[union-attr]


def test_no_egfr_without_sex_or_birthdate_or_wrong_unit() -> None:
    o = [_obs("2160-0", 1.0, "mg/dL", "2020-01-01")]
    assert "egfr" not in build_context({"id": "1", "birthDate": "1960-01-01"}, [], [], o, source="t").labs
    assert "egfr" not in build_context({"id": "1", "gender": "male"}, [], [], o, source="t").labs
    # umol/L is converted to mg/dL (88.4 umol/L = 1.0 mg/dL); an unknown unit is dropped, never guessed
    o2 = [_obs("2160-0", 88.4, "umol/L", "2020-01-01")]
    ctx2 = build_context({"id": "1", "gender": "male", "birthDate": "1960-01-01"}, [], [], o2, source="t")
    assert ctx2.labs["creatinine"][0].value == 1.0 and ctx2.labs["creatinine"][0].unit == "mg/dL"
    o3 = [_obs("2160-0", 88.0, "furlongs", "2020-01-01")]
    ctx3 = build_context({"id": "1", "gender": "male", "birthDate": "1960-01-01"}, [], [], o3, source="t")
    assert "egfr" not in ctx3.labs and "creatinine" not in ctx3.labs and ctx3.dropped_labs


def test_weight_lb_converted() -> None:
    ctx = build_context({"id": "1"}, [], [], [_obs("29463-7", 220, "lb", "2020-01-01")], source="t")
    assert ctx.latest_lab("weight").value == pytest.approx(99.79, abs=0.01)  # type: ignore[union-attr]


def test_inactive_condition_and_status_filtering() -> None:
    def cond(status: str) -> dict:  # type: ignore[type-arg]
        return {
            "clinicalStatus": {"coding": [{"code": status}]},
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "709044004"}]},
        }

    ctx = build_context({"id": "1"}, [], [cond("resolved"), cond("active")], [], source="t")
    assert "ckd" in ctx.conditions
    ctx2 = build_context({"id": "1"}, [], [cond("resolved")], [], source="t")
    assert ctx2.conditions == {}
    med = {"status": "stopped", "medicationCodeableConcept": {"text": "METFORMIN 500MG"}}
    assert build_context({"id": "1"}, [med], [], [], source="t").meds == []


def test_unmapped_meds_tracked() -> None:
    med = {"status": "active", "medicationCodeableConcept": {"text": "GINKGO TAB"}}
    ctx = build_context({"id": "1"}, [med], [], [], source="t")
    assert ctx.unmapped_meds == ["GINKGO TAB"] and ctx.meds == []


def test_drug_from_codeable_rxnorm_and_text() -> None:
    d = drug_from_codeable({"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "1364445"}]})
    assert d.name == "apixaban" and d.strength_mg == 5.0 and d.dose_mg == 5.0
    d2 = drug_from_codeable({"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "5640"}]})
    assert d2.name == "ibuprofen"  # ingredient code directly
    # combination products expand to one drug per ingredient (H2); drug_from_codeable returns the first
    assert drug_from_codeable({"text": "GLYBURIDE/METFORMIN 5/500"}).name == "glyburide"
    assert [d.name for d in drugs_from_codeable({"text": "GLYBURIDE/METFORMIN 5/500"})] == ["glyburide", "metformin"]
    assert not drug_from_codeable(None).mapped


def test_latest_lab_windows() -> None:
    ctx = PatientContext(
        "1",
        "t",
        "male",
        None,
        date(2020, 6, 1),
        labs={
            "egfr": [
                LabValue(date(2020, 1, 1), 50, "u"),
                LabValue(date(2020, 5, 1), 40, "u"),
                LabValue(date(2020, 7, 1), 10, "u"),
            ]
        },
    )
    assert ctx.latest_lab("egfr", 90).value == 40  # type: ignore[union-attr]  # future value ignored
    assert ctx.latest_lab("egfr", 10) is None
    assert ctx.newest_lab_any_age("egfr").value == 40  # type: ignore[union-attr]
    assert ctx.age_years() is None
