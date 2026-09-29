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


# Physiological plausibility bounds after unit normalisation. Outside these a value is a unit/specimen error,
# not a patient: it is dropped (and logged) instead of driving a rule.
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "creatinine": (0.1, 25.0),  # mg/dL
    "potassium": (1.0, 10.0),  # mmol/L
    "lithium": (0.0, 5.0),  # mmol/L
    "weight": (2.0, 400.0),  # kg
    "egfr": (0.0, 250.0),  # mL/min/1.73 m2 (a value of 0 or below is rejected separately: see EXCLUSIVE_LOWER)
}
# Labs where the lower bound itself is not a valid measurement (eGFR 0 is a unit/entry error, not a result).
EXCLUSIVE_LOWER = frozenset({"egfr"})
# Normalised unit for each lab key, and the source units we can convert (factor to the normalised unit).
UNIT_CONVERSIONS: dict[str, dict[str, float]] = {
    "creatinine": {
        "mg/dl": 1.0,
        "mg/100ml": 1.0,
        "umol/l": 1.0 / 88.4,
        "\u00b5mol/l": 1.0 / 88.4,
        "\u03bcmol/l": 1.0 / 88.4,
    },
    "potassium": {"mmol/l": 1.0, "meq/l": 1.0},
    "lithium": {"mmol/l": 1.0, "meq/l": 1.0},
    "weight": {"kg": 1.0, "lb": 0.45359237, "[lb_av]": 0.45359237, "lbs": 0.45359237, "g": 0.001},
}
NORMAL_UNIT = {"creatinine": "mg/dL", "potassium": "mmol/L", "lithium": "mmol/L", "weight": "kg"}


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


@lru_cache(maxsize=1)
def vista_lab_test_iens() -> dict[str, str]:
    """Lab key -> the VEHU lab test IEN we accept for it (rows without an IEN are name-only)."""
    return {
        LAB_KEY_BY_LOINC[r["loinc"]]: r["vista_test_ien"] for r in _rows("vista_lab_tests.csv") if r["vista_test_ien"]
    }


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
