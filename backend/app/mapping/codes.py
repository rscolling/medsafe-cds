"""Condition and lab code maps (SNOMED CT / ICD-9-CM / LOINC) loaded from data/mapping CSVs."""

from __future__ import annotations

import csv
import re
from functools import lru_cache

from app.config import REPO_ROOT

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"
ICD9CM = "http://hl7.org/fhir/sid/icd-9-cm"
UCUM = "http://unitsofmeasure.org"

# LOINC codes that all count as "reported eGFR" (never mixed with our computed value).
EGFR_LOINCS = frozenset({"98979-8", "62238-1", "33914-3", "69405-9", "50044-7", "48642-3", "48643-1"})
CREATININE_LOINCS = frozenset({"2160-0", "38483-4", "14682-9"})
POTASSIUM_LOINCS = frozenset({"2823-3", "6298-4", "39789-3"})
LITHIUM_LOINCS = frozenset({"14334-7", "3719-4"})
WEIGHT_LOINCS = frozenset({"29463-7", "3141-9"})

LAB_KEY_BY_LOINC: dict[str, str] = {
    **dict.fromkeys(EGFR_LOINCS, "egfr"),
    **dict.fromkeys(CREATININE_LOINCS, "creatinine"),
    **dict.fromkeys(POTASSIUM_LOINCS, "potassium"),
    **dict.fromkeys(LITHIUM_LOINCS, "lithium"),
    **dict.fromkeys(WEIGHT_LOINCS, "weight"),
}
PRIMARY_LOINC = {
    "creatinine": "2160-0",
    "potassium": "2823-3",
    "lithium": "14334-7",
    "weight": "29463-7",
    "egfr": "98979-8",
}


def _rows(name: str) -> list[dict[str, str]]:
    with (REPO_ROOT / "data" / "mapping" / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@lru_cache(maxsize=1)
def snomed_conditions() -> dict[str, str]:
    return {r["snomed"]: r["key"] for r in _rows("snomed_conditions.csv")}


@lru_cache(maxsize=1)
def _icd9_prefixes() -> list[tuple[str, str]]:
    rows = [(r["prefix"], r["key"]) for r in _rows("icd9_prefixes.csv")]
    return sorted(rows, key=lambda t: -len(t[0]))  # longest prefix first


@lru_cache(maxsize=1)
def vista_lab_tests() -> dict[str, str]:
    """VistA lab test name -> lab key (creatinine, potassium, ...)."""
    out: dict[str, str] = {}
    for r in _rows("vista_lab_tests.csv"):
        out[r["vista_test_name"].upper()] = LAB_KEY_BY_LOINC[r["loinc"]]
    return out


def condition_key_from_snomed(code: str) -> str | None:
    return snomed_conditions().get(code)


def condition_key_from_icd9(code: str) -> str | None:
    code = code.strip()
    if not re.match(r"^\d{3}", code):
        return None
    for prefix, key in _icd9_prefixes():
        if code.startswith(prefix):
            return key
    return None
