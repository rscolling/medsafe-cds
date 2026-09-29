"""Name-to-RxNorm mapping table tests (VistA drug names are free text)."""

from __future__ import annotations

import csv
import re

import pytest

from app.config import REPO_ROOT
from app.mapping import codes
from app.mapping.drugs import DrugMapper, clinical_drugs, default_mapper, parse_strength_mg

M = default_mapper()


@pytest.mark.parametrize(
    ("text", "ingredient", "rxcui"),
    [
        ("METFORMIN HCL 500MG TAB", "metformin", "6809"),
        ("METFORMIN HYDROCHLORIDE 1000 MG TABLET", "metformin", "6809"),
        ("glucophage 850mg", "metformin", "6809"),
        ("IBUPROFEN 800MG TAB", "ibuprofen", "5640"),
        ("IBUPROFEN TAB", "ibuprofen", "5640"),
        ("LISINOPRIL TAB", "lisinopril", "29046"),
        ("CANDESARTAN CILEXETIL 16MG TAB", "candesartan", "214354"),
        ("FUROSEMIDE 20MG TAB", "furosemide", "4603"),
        ("LITHIUM CARBONATE 300MG CAP", "lithium", "6448"),
        ("DIGOXIN 0.25MG TAB", "digoxin", "3407"),
        ("ASPIRIN 81MG EC TAB", "aspirin", "1191"),
        ("INDOMETHACIN 75MG SR CAPS", "indomethacin", "5781"),
        ("SOD LIOTHYRONINE 25MCG", "liothyronine", "10814"),
        ("Eliquis 5 mg", "apixaban", "1364430"),
        ("XARELTO 20MG", "rivaroxaban", "1114195"),
        ("POTASSIUM CHLORIDE 20MEQ SA TAB", "potassium chloride", "8591"),
    ],
)
def test_maps_to_expected_ingredient(text: str, ingredient: str, rxcui: str) -> None:
    got = M.map_text(text)
    assert got.mapped
    assert got.ingredients[0].name == ingredient
    assert got.ingredients[0].rxcui == rxcui


@pytest.mark.parametrize(
    "text",
    [
        "VITAMIN B COMP W/C & FOLIC (DEXFOL) TAB",
        "GINKGO TAB",
        "CALENDULA CAP/TAB",
        "",
        "ASPIRINFREE GEL",
        "UNOBTAINIUM",
    ],
)
def test_unmapped_stays_unmapped_never_guessed(text: str) -> None:
    assert not M.map_text(text).mapped


def test_token_boundaries_no_substring_matches() -> None:
    assert not M.map_text("NAPROXENISH").mapped
    assert not M.map_text("XLITHIUMX").mapped


def test_combination_products_map_to_all_ingredients() -> None:
    got = M.map_text("GLYBURIDE/METFORMIN 5/500")
    assert {i.name for i in got.ingredients} == {"glyburide", "metformin"}
    assert "sulfonylurea" in got.classes and "biguanide" in got.classes


def test_longest_phrase_wins() -> None:
    assert M.map_text("LITHIUM CARBONATE").ingredients[0].name == "lithium"
    assert len(M.map_text("LITHIUM CARBONATE").ingredients) == 1


@pytest.mark.parametrize(
    ("text", "mg"),
    [
        ("METFORMIN 500MG", 500.0),
        ("LIOTHYRONINE 25MCG", 0.025),
        ("X 0.25 MG", 0.25),
        ("APIXABAN 2.5MG", 2.5),
        ("NO STRENGTH", None),
        ("1 G", 1000.0),
    ],
)
def test_parse_strength(text: str, mg: float | None) -> None:
    assert parse_strength_mg(text) == mg


def test_ingredient_is_its_own_class() -> None:
    assert "apixaban" in M.map_text("APIXABAN").classes
    assert {"nsaid"} <= M.map_text("IBUPROFEN").classes
    assert {"acei", "raas"} <= M.map_text("LISINOPRIL").classes
    assert {"arb", "raas"} <= M.map_text("LOSARTAN").classes


def test_every_vehu_med_name_seen_is_accounted_for() -> None:
    """All 50 free-text names observed in VEHU are either mapped or in the known-unmapped list."""
    import json

    idx = json.loads((REPO_ROOT / "data/vista/recorded/index.json").read_text())
    names = set()
    for e in idx.values():
        if e["rpc"] == "ORWPS ACTIVE":
            names |= {ln[1:].split("^")[2] for ln in e["reply"].split("\r\n") if ln.startswith("~")}
    known_unmapped = {
        "VITAMIN B COMP W/C & FOLIC (DEXFOL) TAB",
        "GINKGO TAB",
        "CALENDULA CAP/TAB",
        "ACIDOPHILUS TAB",
        "CHROMIUM PICOLINATE 200MCG CAP",
        "OMEGA-3 FISH OIL CONC CAPSULE",
    }
    assert len(names) >= 40
    unexpected = {n for n in names if not M.map_text(n).mapped and n not in known_unmapped}
    assert not unexpected, f"add to rxnorm_ingredients.csv or known_unmapped: {sorted(unexpected)}"


def test_mapping_table_integrity() -> None:
    rows = list(csv.DictReader((REPO_ROOT / "data/mapping/rxnorm_ingredients.csv").open()))
    rx = [r["rxcui"] for r in rows]
    assert len(rx) == len(set(rx)), "duplicate RxCUI"
    assert all(re.fullmatch(r"\d+", c) for c in rx)
    for r in rows:
        assert r["classes"], r["ingredient"]


def test_synonym_collisions_are_absent() -> None:
    seen: dict[str, str] = {}
    for r in csv.DictReader((REPO_ROOT / "data/mapping/rxnorm_ingredients.csv").open()):
        for ph in {r["ingredient"].upper(), *filter(None, r["synonyms"].split("|"))}:
            assert seen.setdefault(ph, r["rxcui"]) == r["rxcui"], f"{ph} maps to two RxCUIs"


def test_clinical_drug_table_points_at_known_ingredients() -> None:
    for scd, (ing, _mg, desc) in clinical_drugs().items():
        assert M.ingredient(ing) is not None, (scd, desc)


def test_custom_mapper_from_csv(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "m.csv"
    p.write_text("rxcui,ingredient,classes,synonyms\n1,foo,bar,FOOZ\n")
    assert DrugMapper(p).map_text("FOOZ 5MG").ingredients[0].name == "foo"


def test_condition_and_lab_codes() -> None:
    assert codes.condition_key_from_snomed("709044004") == "ckd"
    assert codes.condition_key_from_snomed("236425005") == "ckd"
    assert codes.condition_key_from_icd9("585.9") == "ckd"
    assert codes.condition_key_from_icd9("250.00") == "diabetes"
    assert codes.condition_key_from_icd9("427.31") == "afib"
    assert codes.condition_key_from_icd9("V12") is None
    assert codes.vista_lab_tests()["CREATININE"] == "creatinine"
    assert codes.LAB_KEY_BY_LOINC["98979-8"] == "egfr"
