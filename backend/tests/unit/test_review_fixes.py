"""Code-review fixes: specimen/plausibility (H3), combinations (H2), units (M7), ties (M9), formulations (M10) ..."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import pytest

from app.adapters.vista import parsing
from app.adapters.vista.fileman import fileman_is_precise, fileman_key, fileman_to_datetime
from app.context import build_context, drugs_from_codeable, parse_datetime
from app.mapping.drugs import default_mapper, parse_strength_mg, strengths_mg_in
from app.model import LabValue, PatientContext
from app.service import CdsService
from tests.conftest import ROOT

M = default_mapper()
LOINC = "http://loinc.org"


def obs(code: str, value: float, unit: str, when: str, **q: Any) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit, **q},
    }


PT = {"id": "p", "gender": "female", "birthDate": "1940-01-01"}


# ------------------------------------------------------------------ H3: urine creatinine is not serum creatinine
def lab_set(specimen: str, spec_ien: str, fm: str, name: str, value: str, ien: str, units: str = "mg/dL") -> str:
    return f"1^CH^{fm}^{spec_ien}^{specimen}^CH 0001 1^PROVIDER^^^{fm}\r\n{ien}^{name}^{value}^^{units}^.9 - 1.4\r\n"


def test_urine_creatinine_is_dropped_but_serum_kept() -> None:
    urine = parsing.parse_lab_set(lab_set("URINE", "71", "3150706.101501", "CREATININE", "208", "173"))
    serum = parsing.parse_lab_set(lab_set("SERUM", "72", "3150706.101500", "CREATININE", "2.1", "173"))
    assert urine and urine.specimen == "URINE" and serum and serum.specimen == "SERUM"
    skipped: dict[str, int] = {}
    got = parsing.lab_sets_to_observations("1", [urine, serum], skipped)
    assert [o["valueQuantity"]["value"] for o in got] == [2.1]
    assert skipped == {"non-systemic-specimen:URINE": 1}


def test_real_vehu_100881_urine_set_does_not_poison_egfr() -> None:
    """DFN 100881 as_of 2015-07-06: urine creatinine (208 mg/dL, collected 1 s after serum) was 'newest'."""
    idx = json.loads((ROOT / "data/vista/recorded/index.json").read_text())
    sets = []
    for e in idx.values():
        if e["rpc"] == "ORWLRR INTERIMG" and json.loads(e["params"])[0] == "100881" and e["reply"].strip():
            s = parsing.parse_lab_set(e["reply"])
            assert s is not None
            sets.append(s)
    assert any(s.specimen == "URINE" for s in sets), "fixture must contain the urine collection"
    observations = parsing.lab_sets_to_observations("100881", sets)
    patient = {"id": "100881", "gender": "female", "birthDate": "1938-04-20"}
    ctx = build_context(patient, [], [], observations, source="vista", as_of=date(2015, 7, 6))
    egfr, cr = ctx.latest_lab("egfr"), ctx.latest_lab("creatinine")
    assert cr is not None and cr.value < 20, cr
    assert egfr is not None and egfr.value > 5, f"eGFR {egfr.value} (urine value leaked in)"
    assert all(v.value > 5 for v in ctx.labs["egfr"]), "no implausible 0.1-0.3 eGFR anywhere in the history"


@pytest.mark.parametrize(
    ("value", "kept"), [("0.05", False), ("25.1", False), ("208", False), ("1.1", True), ("<0.3", True), ("abc", False)]
)
def test_creatinine_plausibility_guard(value: str, kept: bool) -> None:
    s = parsing.parse_lab_set(lab_set("SERUM", "72", "3150706.1015", "CREATININE", value, "173"))
    assert s is not None
    assert bool(parsing.lab_sets_to_observations("1", [s])) is kept


def test_blank_specimen_accepted_wrong_test_ien_and_missing_units_rejected() -> None:
    blank = parsing.parse_lab_set(lab_set("", "", "3150706.1015", "CREATININE", "1.1", "173"))
    other_ien = parsing.parse_lab_set(lab_set("SERUM", "72", "3150706.1015", "CREATININE", "1.1", "999"))
    no_units = parsing.parse_lab_set(lab_set("SERUM", "72", "3150706.1015", "CREATININE", "1.1", "173", units=""))
    assert blank and other_ien and no_units
    assert len(parsing.lab_sets_to_observations("1", [blank])) == 1
    assert parsing.lab_sets_to_observations("1", [other_ien]) == []
    assert parsing.lab_sets_to_observations("1", [no_units]) == []


# ------------------------------------------------------------------ L3: <0.3 / >x are not exact values
def test_inexact_comparator_values_are_not_used_as_exact() -> None:
    s = parsing.parse_lab_set(lab_set("SERUM", "72", "3150706.1015", "CREATININE", "<0.3", "173"))
    assert s is not None
    (o,) = parsing.lab_sets_to_observations("1", [s])
    assert o["valueQuantity"]["comparator"] == "<"
    ctx = build_context(PT, [], [], [o], source="t")
    assert "creatinine" not in ctx.labs and "egfr" not in ctx.labs
    assert any("inexact" in k for k in ctx.dropped_labs)


# ------------------------------------------------------------------ H2: combination products
@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("GLYBURIDE-METFORMIN HCL 2.5/500 TAB", {"glyburide", "metformin"}),
        ("GLIPIZIDE-METFORMIN 2.5-250", {"glipizide", "metformin"}),
        ("LISINOPRIL-HCTZ 20-12.5", {"lisinopril", "hydrochlorothiazide"}),
        ("TRIAMTERENE-HCTZ 37.5-25", {"triamterene", "hydrochlorothiazide"}),
        ("METFORMIN-ER 500MG", {"metformin"}),
        ("METFORMIN500MG TAB", {"metformin"}),
        ("METFORMIN.", {"metformin"}),
        ("POTASSIUM CL 20MEQ", {"potassium chloride"}),
        ("POTASSIUM 20MEQ", {"potassium chloride"}),
        ("K-DUR 20MEQ", {"potassium chloride"}),
        ("LISINOPRIL/HYDROCHLOROTHIAZIDE 20/12.5MG TAB", {"lisinopril", "hydrochlorothiazide"}),
        ("GLYBURIDE/METFORMIN", {"glyburide", "metformin"}),
    ],
)
def test_combination_and_hyphen_forms_map_to_every_ingredient(text: str, names: set[str]) -> None:
    assert {i.name for i in M.map_text(text).ingredients} == names
    assert {d.name for d in drugs_from_codeable({"text": text}) if d.mapped} == names


def test_combination_expands_to_one_drugref_per_ingredient_with_own_strength() -> None:
    a, b = drugs_from_codeable({"text": "LISINOPRIL-HCTZ 20-12.5"})
    assert (a.name, a.strength_mg, b.name, b.strength_mg) == ("lisinopril", 20.0, "hydrochlorothiazide", 12.5)
    a, b = drugs_from_codeable({"text": "GLYBURIDE-METFORMIN HCL 2.5/500 TAB"})
    assert (a.strength_mg, b.strength_mg) == (2.5, 500.0)


def test_losartan_potassium_is_not_potassium_chloride() -> None:
    assert [i.name for i in M.map_text("LOSARTAN POTASSIUM 50MG").ingredients] == ["losartan"]
    assert not M.map_text("POTASSIUM CITRATE 10MEQ").mapped


def draft(text: str) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": "d1",
        "status": "draft",
        "medicationCodeableConcept": {"text": text},
    }


def med(text: str, i: int = 1) -> dict[str, Any]:
    return {
        "resourceType": "MedicationRequest",
        "id": f"m{i}",
        "status": "active",
        "medicationCodeableConcept": {"text": text},
    }


def ctx_with(meds: list[str], creatinine: float = 2.5) -> PatientContext:
    return build_context(
        PT,
        [med(t, i) for i, t in enumerate(meds)],
        [],
        [obs("2160-0", creatinine, "mg/dL", "2026-09-01")],
        source="t",
    )


def alert_ids(service: CdsService, ctx: PatientContext, text: str, mode: str = "context") -> set[str]:
    return {a.rule_id for a in service.evaluate_ctx(ctx, draft(text), mode).result.alerts}


def test_combination_draft_fires_triple_whammy_like_separate_drugs(service) -> None:  # type: ignore[no-untyped-def]
    """LISINOPRIL/HCTZ + current ibuprofen == lisinopril + HCTZ + ibuprofen (review verified this was silent)."""
    separate = alert_ids(service, ctx_with(["HYDROCHLOROTHIAZIDE 25MG", "IBUPROFEN 800MG"]), "LISINOPRIL 20MG")
    combo = alert_ids(service, ctx_with(["IBUPROFEN 800MG"]), "LISINOPRIL/HYDROCHLOROTHIAZIDE 20/12.5MG TAB")
    hyphen = alert_ids(service, ctx_with(["IBUPROFEN 800MG"]), "LISINOPRIL-HCTZ 20-12.5")
    assert "nsaid-raas-diuretic-aki" in separate
    assert combo == separate == hyphen


def test_combination_current_med_counts_for_each_class(service) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_with(["LISINOPRIL-HCTZ 20-12.5"])
    assert {m.name for m in ctx.meds} == {"lisinopril", "hydrochlorothiazide"}
    assert "nsaid-raas-diuretic-aki" in alert_ids(service, ctx, "IBUPROFEN 800MG")


def test_glyburide_metformin_draft_fires_metformin_rule(service) -> None:  # type: ignore[no-untyped-def]
    assert "metformin-low-egfr" in alert_ids(
        service, ctx_with([], creatinine=2.5), "GLYBURIDE-METFORMIN HCL 2.5/500 TAB"
    )


@pytest.mark.parametrize("text", ["GLUCOVANCE-UNKNOWN THING", "", "UNOBTAINIUM 5MG"])
def test_unidentified_draft_is_reported_not_silent(service, text: str) -> None:  # type: ignore[no-untyped-def]
    ev = service.evaluate_ctx(ctx_with([]), draft(text), "context")
    assert ev.unchecked_reason and "could not be identified" in ev.unchecked_reason and not ev.result.alerts


def test_unchecked_draft_returns_info_card_over_the_api(service) -> None:  # type: ignore[no-untyped-def]
    from tests.contract.test_cds_hooks_contract import hook_body
    from tests.unit.test_hardening import make

    body = hook_body("hand-01", "861007")
    body["context"]["draftOrders"]["entry"][0]["resource"]["medicationCodeableConcept"] = {"text": "UNOBTAINIUM 5MG"}
    r = make(service).post("/cds-services/medsafe-order-sign", json=body)
    (card,) = r.json()["cards"]
    assert card["indicator"] == "info" and "not performed" in card["summary"]
    assert card["extension"]["org.medsafe.unchecked"] is True and "Prototype, not clinical advice" in card["detail"]


# ------------------------------------------------------------------ M10: formulations / partial combos
@pytest.mark.parametrize(
    "text",
    ["DICLOFENAC 1% GEL", "IBUPROFEN 5% CREAM", "KETOROLAC OPHTH SOLN", "HEPARIN LOCK FLUSH", "DIGOXIN IMMUNE FAB"],
)
def test_non_systemic_products_are_not_systemic_ingredients(text: str) -> None:
    m = M.map_text(text)
    assert not m.mapped and m.excluded


def test_topical_nsaid_does_not_trigger_lithium_or_triple_whammy(service) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_with(["LITHIUM CARBONATE 300MG"])
    assert "lithium-interacting-drugs" in alert_ids(service, ctx, "IBUPROFEN 800MG")
    ev = service.evaluate_ctx(ctx, draft("DICLOFENAC 1% GEL"), "context")
    assert not ev.result.alerts and "non-systemic" in (ev.unchecked_reason or "")
    ctx2 = ctx_with(["DICLOFENAC 1% GEL", "LITHIUM CARBONATE 300MG"])
    assert ctx2.excluded_meds == ["DICLOFENAC 1% GEL"] and {m.name for m in ctx2.meds} == {"lithium"}


def test_transdermal_patch_is_still_systemic() -> None:
    assert M.map_text("NICOTINE 14MG/24HR PATCH").mapped


@pytest.mark.parametrize("text", ["SACUBITRIL/VALSARTAN", "AMLODIPINE/BENAZEPRIL", "ASPIRIN/DIPYRIDAMOLE"])
def test_partially_recognised_combination_is_flagged(text: str) -> None:
    m = M.map_text(text)
    assert m.mapped and m.partial
    assert all(d.partial for d in drugs_from_codeable({"text": text}))
    assert build_context(PT, [med(text)], [], [], source="t").partial_meds == [text]


def test_fully_recognised_combination_is_not_partial() -> None:
    assert not M.map_text("LISINOPRIL/HYDROCHLOROTHIAZIDE 20/12.5MG TAB").partial
    assert not M.map_text("METFORMIN HCL 500MG TAB").partial


def test_partial_draft_carries_a_reason(service) -> None:  # type: ignore[no-untyped-def]
    ev = service.evaluate_ctx(ctx_with([]), draft("AMLODIPINE/BENAZEPRIL 5/20"), "context")
    assert ev.unchecked_reason and "only part of this combination" in ev.unchecked_reason


# ------------------------------------------------------------------ L4: strengths
@pytest.mark.parametrize(
    ("text", "mg"),
    [
        ("DIGOXIN .25MG", 0.25),
        ("DIGOXIN .05MG TAB", 0.05),
        ("DIGOXIN 0.125 MG", 0.125),
        ("AMOXICILLIN 500MG/5ML SUSP", None),
        ("AMOXICILLIN 250 MG/5 ML", None),
        ("ASPIRIN 1 GM", 1000.0),
        ("XYZ 2G", 2000.0),
        ("L-THYROXINE 50MCG", 0.05),
        ("METFORMIN 500MG", 500.0),
        ("NO NUMBER", None),
    ],
)
def test_parse_strength_mg(text: str, mg: float | None) -> None:
    assert parse_strength_mg(text) == mg


def test_strengths_list_and_per_ingredient_assignment() -> None:
    assert strengths_mg_in("A 5MG B 10MG") == [5.0, 10.0]
    assert M.map_text("GLYBURIDE/METFORMIN 5/500").strengths_mg == (5.0, 500.0)
    # a single number is never copied to every ingredient of a combination
    assert M.map_text("LISINOPRIL-HCTZ 20MG").strengths_mg == (None, None)


# ------------------------------------------------------------------ M1 / M7 / M9 / L2
def test_stale_reported_egfr_does_not_block_computing_from_newer_creatinine() -> None:
    o = [obs("98979-8", 80.0, "mL/min/1.73m2", "2019-01-01"), obs("2160-0", 3.0, "mg/dL", "2026-08-01")]
    ctx = build_context(PT, [], [], o, source="t")
    latest = ctx.latest_lab("egfr")
    assert latest is not None and latest.source == "computed" and latest.when == date(2026, 8, 1) and latest.value < 30
    assert [v.source for v in ctx.labs["egfr"]] == ["reported", "computed"]


def test_reported_egfr_same_day_wins_over_computed() -> None:
    o = [obs("98979-8", 55.0, "mL/min/1.73m2", "2026-08-01"), obs("2160-0", 3.0, "mg/dL", "2026-08-01")]
    ctx = build_context(PT, [], [], o, source="t")
    assert [(v.source, v.value) for v in ctx.labs["egfr"]] == [("reported", 55.0)]


def test_creatinine_units_are_normalised_for_doac_checks(service) -> None:  # type: ignore[no-untyped-def]
    """80 umol/L is 0.9 mg/dL: Cockcroft-Gault must not see '80 mg/dL' (review: CrCl 1 mL/min, false alert)."""
    afib = [
        {"resourceType": "Condition", "code": {"coding": [{"system": "http://snomed.info/sct", "code": "49436004"}]}}
    ]
    pt = {"id": "p", "gender": "male", "birthDate": "1950-01-01"}
    o = [obs("2160-0", 80, "umol/L", "2026-09-01"), obs("29463-7", 80, "kg", "2026-09-01")]
    ctx = build_context(pt, [], afib, o, source="t")
    assert ctx.latest_lab("creatinine").value == pytest.approx(0.905, abs=0.001)  # type: ignore[union-attr]
    ids = {a.rule_id for a in service.evaluate_ctx(ctx, draft("RIVAROXABAN 20MG"), "context").result.alerts}
    assert "rivaroxaban-renal-dose" not in ids
    bad = build_context(
        pt, [], afib, [obs("2160-0", 80, "mystery", "2026-09-01"), obs("29463-7", 80, "kg", "2026-09-01")], source="t"
    )
    assert "creatinine" not in bad.labs and bad.dropped_labs


def test_potassium_meq_and_weight_lb_units_normalised() -> None:
    ctx = build_context(
        PT, [], [], [obs("2823-3", 4.5, "mEq/L", "2026-09-01"), obs("29463-7", 200, "lb", "2026-09-01")], source="t"
    )
    assert ctx.latest_lab("potassium").unit == "mmol/L"  # type: ignore[union-attr]
    assert ctx.latest_lab("weight").value == pytest.approx(90.72, abs=0.01)  # type: ignore[union-attr]


def test_same_day_ties_use_time_not_input_order() -> None:
    early = obs("2160-0", 1.0, "mg/dL", "2026-09-01T08:00:00")
    late = obs("2160-0", 200.0 / 100, "mg/dL", "2026-09-01T09:30:00")
    for order in ([early, late], [late, early]):
        ctx = build_context(PT, [], [], order, source="t")
        assert ctx.latest_lab("creatinine").value == 2.0  # type: ignore[union-attr]
        assert [v.value for v in ctx.labs["creatinine"]] == [1.0, 2.0]


def test_exact_same_instant_tie_is_deterministic_and_cautious() -> None:
    a, b = LabValue(date(2026, 9, 1), 1.0, "u"), LabValue(date(2026, 9, 1), 5.0, "u")
    for series in ([a, b], [b, a]):
        c = PatientContext("p", "t", "male", None, date(2026, 9, 2), labs={"creatinine": series, "egfr": series})
        assert c.latest_lab("creatinine").value == 5.0  # type: ignore[union-attr]  # higher creatinine
        assert c.latest_lab("egfr").value == 1.0  # type: ignore[union-attr]  # lower eGFR


def test_bad_lab_does_not_abort_the_whole_context() -> None:
    """A creatinine dated before the DOB (age < 0) used to raise and fail the whole hook open."""
    o = [obs("2160-0", 1.0, "mg/dL", "1930-01-01"), obs("2160-0", 1.2, "mg/dL", "2026-09-01")]
    ctx = build_context(PT, [], [], o, source="t")
    assert [v.when for v in ctx.labs["egfr"]] == [date(2026, 9, 1)]
    assert any("not computable" in k for k in ctx.dropped_labs)


# ------------------------------------------------------------------ M8: as-of policy
def test_explicit_as_of_and_today_policy_are_recorded() -> None:
    o = [obs("2160-0", 3.0, "mg/dL", "2021-03-01")]
    anchored = build_context(PT, [], [], o, source="t")
    today = build_context(PT, [], [], o, source="t", as_of_policy="today", today=date(2026, 9, 29))
    explicit = build_context(PT, [], [], o, source="t", as_of=date(2021, 4, 1))
    assert (anchored.as_of_policy, today.as_of_policy, explicit.as_of_policy) == ("anchored", "today", "explicit")
    assert today.as_of == date(2026, 9, 29) and today.latest_lab("egfr", 90) is None


def test_stale_creatinine_is_a_data_gap_not_critical_under_today_policy(service) -> None:  # type: ignore[no-untyped-def]
    o = [obs("2160-0", 3.0, "mg/dL", "2021-03-01")]
    stale = build_context(PT, [], [], o, source="t", as_of_policy="today", today=date(2026, 9, 29))
    (alert,) = service.evaluate_ctx(stale, draft("METFORMIN 500MG"), "context").result.alerts
    assert alert.data_gap and alert.indicator == "info"
    anchored = build_context(PT, [], [], o, source="t")
    (crit,) = service.evaluate_ctx(anchored, draft("METFORMIN 500MG"), "context").result.alerts
    assert not crit.data_gap and crit.indicator == "critical"  # old behaviour, only under the anchored demo policy


# ------------------------------------------------------------------ L1 / L14: FileMan
@pytest.mark.parametrize("bad", ["3150810.ab", "3150810.12x4", "abc", "315081", "31508100", "3151310", ""])
def test_fileman_garbage_is_none_not_an_exception(bad: str) -> None:
    assert fileman_to_datetime(bad) is None


def test_fileman_valid_forms_and_precision() -> None:
    assert fileman_to_datetime("3150810.1237") == datetime(2015, 8, 10, 12, 37)
    assert fileman_to_datetime("3150810.123") == datetime(2015, 8, 10, 12, 30)
    assert fileman_to_datetime("3120601.08") == datetime(2012, 6, 1, 8, 0)
    assert fileman_to_datetime("2380420") == datetime(1938, 4, 20)
    assert fileman_is_precise("3150810.1") and not fileman_is_precise("3150000") and not fileman_is_precise("3150800")


def test_imprecise_dob_becomes_year_only_so_age_is_unknown() -> None:
    p = parsing.parse_patient_select("1", "NAME,X^F^3150000^000000000^^^")
    assert p["birthDate"] == "2015"
    ctx = build_context({**p}, [], [], [obs("2160-0", 1.0, "mg/dL", "2026-09-01")], source="t")
    assert ctx.birth_date is None and "egfr" not in ctx.labs  # a data gap, not an age wrong by up to a year


def test_fileman_key_is_numeric_not_lexicographic() -> None:
    """FileMan times are decimal fractions: .1 and .10 are the same instant, .9 is later than .10."""
    assert fileman_key("3150810.1") == fileman_key("3150810.10")
    assert "3150810.1" != "3150810.10"  # the string comparison would call these different
    assert fileman_key("3150810.9") > fileman_key("3150810.10")  # type: ignore[operator]
    assert fileman_key("3150810.9") > fileman_key("3150809.99")  # type: ignore[operator]
    assert fileman_key("junk") is None


def test_parse_datetime_and_naive_normalisation() -> None:
    assert parse_datetime("2026-09-01T09:30:00Z") == datetime(2026, 9, 1, 9, 30)
    assert parse_datetime("2026-09-01") == datetime(2026, 9, 1)
    assert parse_datetime(5) is None and parse_datetime("nope") is None and parse_datetime(None) is None
