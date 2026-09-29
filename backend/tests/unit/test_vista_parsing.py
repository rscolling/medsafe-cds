"""Parsers tested against REAL replies captured from worldvista/vehu (fixtures/vehu_real_replies.json)."""

from __future__ import annotations

from datetime import date

import pytest

from app.adapters.vista import parsing
from app.adapters.vista.fileman import date_to_fileman, fileman_to_date, fileman_to_datetime


def test_fileman_dates() -> None:
    assert fileman_to_datetime("3150810.1237").isoformat() == "2015-08-10T12:37:00"  # type: ignore[union-attr]
    assert fileman_to_date("2380420") == date(1938, 4, 20)
    assert fileman_to_date("3141229") == date(2014, 12, 29)
    assert fileman_to_date("") is None
    assert fileman_to_date("garbage") is None
    assert fileman_to_date("3151340") is None  # month 13
    assert fileman_to_date("3150000") == date(2015, 1, 1)  # imprecise FileMan date
    assert date_to_fileman(date(2015, 8, 10)) == "3150810"


def test_patient_select(vehu_real: dict[str, str]) -> None:
    p = parsing.parse_patient_select("100881", vehu_real["select_100881"])
    assert p["gender"] == "female" and p["birthDate"] == "1938-04-20"
    assert "ssn" not in str(p).lower()
    with pytest.raises(ValueError, match="unexpected"):
        parsing.parse_patient_select("1", "")


def test_active_meds_pending_is_current(vehu_real: dict[str, str]) -> None:
    (m,) = parsing.parse_active_meds(vehu_real["meds_100157"])
    assert (m.order_type, m.status, m.name) == ("OP", "PENDING", "METFORMIN HCL 500MG TAB")
    assert "TWICE A DAY" in m.sig
    res = parsing.med_to_medication_request("100157", m)
    assert res is not None and res["status"] == "active"
    assert res["medicationCodeableConcept"]["text"] == "METFORMIN HCL 500MG TAB"


def test_active_meds_non_va(vehu_real: dict[str, str]) -> None:
    (m,) = parsing.parse_active_meds(vehu_real["meds_100151"])
    assert (m.order_type, m.status) == ("NV", "ACTIVE")


def test_discontinued_status_is_not_current() -> None:
    m = parsing.VistaMed("OP", "1", "X", "DISCONTINUED")
    assert parsing.med_to_medication_request("1", m) is None
    m2 = parsing.VistaMed("OP", "1", "X", "HOLD")
    assert parsing.med_to_medication_request("1", m2)["status"] == "on-hold"  # type: ignore[index]


def test_empty_meds() -> None:
    assert parsing.parse_active_meds("") == []


def test_problems(vehu_real: dict[str, str]) -> None:
    probs = parsing.parse_problems("100881", vehu_real["problems_100881"])
    assert len(probs) == 5
    ckd = next(p for p in probs if "renal" in p["code"]["text"].lower())
    systems = {c["system"]: c["code"] for c in ckd["code"]["coding"]}
    assert systems["http://snomed.info/sct"] == "236425005"
    assert systems["http://hl7.org/fhir/sid/icd-9-cm"] == "585.9"
    assert ckd["onsetDateTime"] == "2014-06-30"


def test_no_problems(vehu_real: dict[str, str]) -> None:
    assert parsing.parse_problems("1", vehu_real["problems_none"]) == []


def test_lab_set_and_observations(vehu_real: dict[str, str]) -> None:
    s = parsing.parse_lab_set(vehu_real["labs_100881_latest"])
    assert s is not None and s.collected == "3150810.1237"
    assert {r["name"] for r in s.results} >= {"CREATININE", "POTASSIUM", "GLUCOSE"}
    obs = parsing.lab_sets_to_observations("100881", [s])
    by_loinc = {o["code"]["coding"][0]["code"]: o for o in obs}
    assert by_loinc["2160-0"]["valueQuantity"]["value"] == 2.1
    assert by_loinc["2160-0"]["effectiveDateTime"].startswith("2015-08-10")
    assert "2823-3" in by_loinc  # potassium mapped, glucose dropped
    assert len(obs) == 2


def test_lab_set_empty_and_garbage() -> None:
    assert parsing.parse_lab_set("") is None
    assert parsing.parse_lab_set("x") is None
    assert parsing.lab_sets_to_observations("1", [parsing.LabSet("bad", [])]) == []


def test_lab_non_numeric_values_are_skipped() -> None:
    s = parsing.LabSet(
        "3150810.1200",
        [
            {
                "test_ien": "173",
                "name": "CREATININE",
                "value": "pending",
                "flag": "",
                "units": "mg/dL",
                "range": "",
            }
        ],
    )
    assert parsing.lab_sets_to_observations("1", [s]) == []
    s2 = parsing.LabSet(
        "3150810.1200",
        [
            {
                "test_ien": "173",
                "name": "CREATININE",
                "value": "<0.2",
                "flag": "",
                "units": "mg/dL",
                "range": "",
            }
        ],
    )
    assert parsing.lab_sets_to_observations("1", [s2])[0]["valueQuantity"]["value"] == 0.2


def test_weight_from_vitals(vehu_real: dict[str, str]) -> None:
    (w,) = parsing.parse_weight("100881", vehu_real["vitals_100881"])
    assert w["valueQuantity"] == {
        "value": 64.41,
        "unit": "kg",
        "system": "http://unitsofmeasure.org",
    }
    lb_only = "1^WT^150^3150810.1534^150 lb^^\r\n"
    assert parsing.parse_weight("1", lb_only)[0]["valueQuantity"]["value"] == pytest.approx(68.04, abs=0.01)


def test_newold(vehu_real: dict[str, str]) -> None:
    assert parsing.newest_oldest(vehu_real["newold_100881"]) == (
        date(2015, 8, 10),
        date(2012, 6, 1),
    )
    assert parsing.newest_oldest("^") == (None, None)
