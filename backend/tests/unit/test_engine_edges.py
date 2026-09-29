from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from app.context import drug_from_codeable
from app.model import DrugRef, LabValue, PatientContext
from app.rules import checks
from app.rules.engine import RuleLoadError, RulesEngine, load_rules
from app.rules.schema import Predicate

D = date(2026, 9, 1)


def ctx(**kw) -> PatientContext:  # type: ignore[no-untyped-def]
    base = {"patient_id": "p", "source": "t", "sex": "female", "birth_date": date(1940, 1, 1), "as_of": D}
    base.update(kw)
    return PatientContext(**base)  # type: ignore[arg-type]


def drug(text: str, mg: float | None = None) -> DrugRef:
    d = drug_from_codeable({"text": text})
    return DrugRef(d.raw, d.rxcui, d.name, d.classes, d.strength_mg, mg if mg is not None else d.strength_mg)


def lab(days: int, v: float, unit: str = "u") -> LabValue:
    return LabValue(D - timedelta(days=days), v, unit)


# ---------------------------------------------------------------- loading
def write_rule(tmp: Path, name: str, **over) -> None:  # type: ignore[no-untyped-def]
    base = yaml.safe_load(Path("../rules/metformin-low-egfr.yaml").read_text())
    base.update(over)
    (tmp / name).write_text(yaml.safe_dump(base))


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(RuleLoadError, match="no rules"):
        load_rules(tmp_path)
    (tmp_path / "bad.yaml").write_text("id: x\n")
    with pytest.raises(RuleLoadError, match="bad.yaml"):
        load_rules(tmp_path)
    (tmp_path / "bad.yaml").unlink()
    write_rule(tmp_path, "a.yaml")
    write_rule(tmp_path, "b.yaml")
    with pytest.raises(RuleLoadError, match="duplicate"):
        load_rules(tmp_path)
    (tmp_path / "b.yaml").unlink()
    write_rule(tmp_path, "a.yaml", context={"check": "does_not_exist"})
    with pytest.raises(RuleLoadError, match="unknown check"):
        load_rules(tmp_path)


def test_schema_rejects_non_https_source_and_bad_version(tmp_path: Path) -> None:
    write_rule(tmp_path, "a.yaml", version="1.0")
    with pytest.raises(RuleLoadError):
        load_rules(tmp_path)
    base = yaml.safe_load(Path("../rules/metformin-low-egfr.yaml").read_text())
    base["card"]["source"]["url"] = "http://insecure.example"
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(base))
    with pytest.raises(RuleLoadError):
        load_rules(tmp_path)


def test_bad_card_template_is_reported(tmp_path: Path) -> None:
    base = yaml.safe_load(Path("../rules/metformin-low-egfr.yaml").read_text())
    base["card"]["fire"]["summary"] = "needs {nonexistent}"
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(base))
    eng = RulesEngine.from_dir(tmp_path)
    c = ctx(labs={"egfr": [lab(5, 20, "mL/min/1.73m2")]})
    with pytest.raises(RuleLoadError, match="nonexistent"):
        eng.evaluate(c, drug("METFORMIN 500MG"), "context")
    with pytest.raises(ValueError, match="unknown mode"):
        eng.evaluate(c, drug("METFORMIN 500MG"), "nope")


def test_engine_requires_distinct_drugs_per_group() -> None:
    eng = RulesEngine(load_rules(Path("../rules")))
    only_nsaid = ctx(meds=[drug("IBUPROFEN 800MG")])
    order = drug("NAPROXEN 500MG")
    assert not [a for a in eng.evaluate(only_nsaid, order, "baseline").alerts if a.rule_id == "nsaid-raas-diuretic-aki"]
    full = ctx(meds=[drug("LISINOPRIL 10MG"), drug("FUROSEMIDE 20MG")])
    ids = [a.rule_id for a in eng.evaluate(full, order, "baseline").alerts]
    assert "nsaid-raas-diuretic-aki" in ids


def test_draft_order_must_participate_in_the_match() -> None:
    eng = RulesEngine(load_rules(Path("../rules")))
    already = ctx(meds=[drug("IBUPROFEN 800MG"), drug("LISINOPRIL 10MG"), drug("FUROSEMIDE 20MG")])
    unrelated = drug("ACETAMINOPHEN 325MG")
    assert eng.evaluate(already, unrelated, "baseline").alerts == ()


def test_catalog() -> None:
    cat = RulesEngine(load_rules(Path("../rules"))).catalog()
    assert {c["id"] for c in cat} >= {"metformin-low-egfr"} and all(c["source"].startswith("https") for c in cat)


# ---------------------------------------------------------------- checks
P_MET = {"window_days": 90, "contraindicated_below": 30, "caution_below": 45}


def test_metformin_bands() -> None:
    o = drug("METFORMIN 500MG")
    assert checks.metformin_egfr(ctx(labs={"egfr": [lab(5, 25)]}), o, [o], P_MET).status == "fire"
    assert checks.metformin_egfr(ctx(labs={"egfr": [lab(5, 25)]}), o, [o], P_MET).indicator == "critical"
    assert checks.metformin_egfr(ctx(labs={"egfr": [lab(5, 40)]}), o, [o], P_MET).indicator is None
    assert checks.metformin_egfr(ctx(labs={"egfr": [lab(5, 60)]}), o, [o], P_MET).status == "quiet"
    assert checks.metformin_egfr(ctx(labs={"egfr": [lab(120, 20)]}), o, [o], P_MET).status == "data_gap"  # stale


P_AP = {
    "age_gte": 80,
    "weight_kg_lte": 60,
    "creatinine_mg_dl_gte": 1.5,
    "creatinine_window_days": 90,
    "weight_window_days": 365,
    "criteria_needed": 2,
}


def afib(**kw):  # type: ignore[no-untyped-def]
    return ctx(conditions={"afib": "AF"}, **kw)


def test_apixaban_paths() -> None:
    a5, a25 = drug("APIXABAN 5MG", 5), drug("APIXABAN 2.5MG", 2.5)
    old = date(1940, 1, 1)
    assert checks.apixaban_dose(afib(birth_date=old), a25, [a25], P_AP).status == "quiet"
    assert checks.apixaban_dose(ctx(birth_date=old), a5, [a5], P_AP).status == "quiet"  # no AF
    nodose = drug("APIXABAN", None)
    nodose = DrugRef(nodose.raw, nodose.rxcui, nodose.name, nodose.classes, None, None)
    assert checks.apixaban_dose(afib(), nodose, [nodose], P_AP).status == "data_gap"
    # age 86 + weight 55 -> fire
    c = afib(birth_date=old, labs={"weight": [lab(10, 55, "kg")]})
    assert checks.apixaban_dose(c, a5, [a5], P_AP).status == "fire"
    # age 86 only known + missing creatinine/weight -> gap (2 could still be met)
    assert checks.apixaban_dose(afib(birth_date=old), a5, [a5], P_AP).status == "data_gap"
    # young, heavy, good creatinine -> quiet
    c = afib(
        birth_date=date(1975, 1, 1),
        labs={"weight": [lab(10, 95, "kg")], "creatinine": [lab(10, 0.9, "mg/dL")]},
    )
    assert checks.apixaban_dose(c, a5, [a5], P_AP).status == "quiet"
    # creatinine + weight met, unknown age
    c = afib(birth_date=None, labs={"weight": [lab(10, 50, "kg")], "creatinine": [lab(10, 2.0, "mg/dL")]})
    r = checks.apixaban_dose(c, a5, [a5], P_AP)
    assert r.status == "fire" and r.facts["criteria_met"] == "2"


P_RV = {"crcl_window_days": 90, "weight_window_days": 365, "reduce_below": 50, "avoid_below": 15}


def test_rivaroxaban_paths() -> None:
    r20, r15 = drug("RIVAROXABAN 20MG", 20), drug("RIVAROXABAN 15MG", 15)
    assert checks.rivaroxaban_renal(afib(), r15, [r15], P_RV).status == "quiet"
    assert checks.rivaroxaban_renal(ctx(), r20, [r20], P_RV).status == "quiet"  # no AF
    nodose = DrugRef("RIVAROXABAN", "1114195", "rivaroxaban", frozenset(), None, None)
    assert checks.rivaroxaban_renal(afib(), nodose, [nodose], P_RV).status == "data_gap"
    assert checks.rivaroxaban_renal(afib(), r20, [r20], P_RV).status == "data_gap"
    assert checks.rivaroxaban_renal(afib(sex="unknown"), r20, [r20], P_RV).status == "data_gap"
    low = afib(labs={"creatinine": [lab(5, 4.0, "mg/dL")], "weight": [lab(5, 50, "kg")]})
    out = checks.rivaroxaban_renal(low, r20, [r20], P_RV)
    assert out.status == "fire" and "below 15" in out.facts["extra"]
    ok = afib(
        sex="male",
        birth_date=date(1975, 1, 1),
        labs={"creatinine": [lab(5, 0.9, "mg/dL")], "weight": [lab(5, 90, "kg")]},
    )
    assert checks.rivaroxaban_renal(ok, r20, [r20], P_RV).status == "quiet"


def test_triple_whammy_and_num() -> None:
    p = {"window_days": 90, "egfr_below": 60}
    o = drug("IBUPROFEN 800MG")
    assert checks.triple_whammy_renal(ctx(labs={"egfr": [lab(5, 40)]}), o, [o], p).status == "fire"
    assert checks.triple_whammy_renal(ctx(labs={"egfr": [lab(5, 80)]}), o, [o], p).status == "quiet"
    assert checks.triple_whammy_renal(ctx(), o, [o], p).status == "data_gap"
    assert checks.num(None) == "unknown" and checks.num(2.50) == "2.5"


def test_predicate_helpers() -> None:
    from app.rules.engine import _predicate

    c = ctx(labs={"potassium": [lab(10, 4.2)]})
    ok, text = _predicate(c, Predicate(lab="potassium", op="<=", value=5.0, within_days=30))
    assert ok and "4.2" in text
    ok, text = _predicate(c, Predicate(lab="potassium", op=">", value=5.0, within_days=30))
    assert not ok
    ok, text = _predicate(c, Predicate(lab="egfr", op=">=", value=60, within_days=90))
    assert not ok and "no egfr" in text
