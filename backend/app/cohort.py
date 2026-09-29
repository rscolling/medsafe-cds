"""Deterministic Synthea-style synthetic cohort (no download needed).

This is NOT output of the real Synthea generator; it is a small seeded generator that produces
FHIR-shaped patients with plausible conditions, medications and renal labs so the baseline vs
context-aware comparison has a population to run over. ``data/synthea/`` contains the config to
generate real Synthea patients instead (optional, see docs/integration-notes.md).
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any

from app.clinical.egfr import ckd_epi_2021

RX = "http://www.nlm.nih.gov/research/umls/rxnorm"
SCT = "http://snomed.info/sct"
LOINC = "http://loinc.org"
RECORD_DATE = date(2026, 9, 1)

_MEDS = {
    "metformin": ("861007", "metformin hydrochloride 500 MG Oral Tablet", 500.0),
    "lisinopril": ("314076", "lisinopril 10 MG Oral Tablet", 10.0),
    "losartan": ("979492", "losartan potassium 50 MG Oral Tablet", 50.0),
    "furosemide": ("310429", "furosemide 20 MG Oral Tablet", 20.0),
    "hctz": ("310798", "hydrochlorothiazide 25 MG Oral Tablet", 25.0),
    "ibuprofen": ("197807", "ibuprofen 800 MG Oral Tablet", 800.0),
    "naproxen": ("198014", "naproxen 500 MG Oral Tablet", 500.0),
    "apixaban5": ("1364445", "apixaban 5 MG Oral Tablet", 5.0),
    "rivaroxaban20": ("1232086", "rivaroxaban 20 MG Oral Tablet", 20.0),
    "kcl": ("1801294", "potassium chloride 20 MEQ Extended Release Oral Tablet", 0.0),
    "spironolactone": ("9997", "spironolactone", 25.0),
    "lithium": ("42351", "lithium carbonate", 300.0),
}
_CONDS = {
    "t2dm": ("44054006", "Type 2 diabetes mellitus"),
    "ckd": ("709044004", "Chronic kidney disease"),
    "afib": ("49436004", "Atrial fibrillation"),
    "htn": ("38341003", "Hypertensive disorder"),
    "hf": ("84114007", "Heart failure"),
}
# orders the cohort run tries for each patient (draft order key)
CANDIDATE_ORDERS = [
    "metformin",
    "ibuprofen",
    "naproxen",
    "apixaban5",
    "rivaroxaban20",
    "kcl",
    "spironolactone",
    "lithium",
]


def med_resource(pid: str, key: str, status: str = "active", idx: int = 0) -> dict[str, Any]:
    rx, disp, mg = _MEDS[key]
    res: dict[str, Any] = {
        "resourceType": "MedicationRequest",
        "id": f"{pid}-med-{key}-{idx}",
        "status": status,
        "intent": "order",
        "subject": {"reference": f"Patient/{pid}"},
        "medicationCodeableConcept": {
            "coding": [{"system": RX, "code": rx, "display": disp}],
            "text": disp,
        },
    }
    if mg:
        res["dosageInstruction"] = [{"doseAndRate": [{"doseQuantity": {"value": mg, "unit": "mg"}}]}]
    return res


def _obs(pid: str, name: str, loinc: str, disp: str, value: float, unit: str, when: date) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": f"{pid}-{name}-{when.isoformat()}",
        "status": "final",
        "code": {"coding": [{"system": LOINC, "code": loinc, "display": disp}], "text": disp},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when.isoformat(),
        "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org"},
    }


def generate_cohort(n: int = 300, seed: int = 20260929) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311  # nosec B311 - synthetic data, not security
    docs: list[dict[str, Any]] = []
    for i in range(1, n + 1):
        pid = f"syn-{i:04d}"
        sex = rng.choice(["male", "female"])
        age = int(min(95, max(30, rng.gauss(64, 14))))
        birth = date(RECORD_DATE.year - age, rng.randint(1, 12), rng.randint(1, 28))
        conds = {
            c
            for c, p in (
                ("t2dm", 0.28),
                ("htn", 0.55),
                ("hf", 0.10),
                ("afib", 0.09 + 0.002 * max(0, age - 60)),
            )
            if rng.random() < p
        }
        ckd_p = 0.05 + 0.006 * max(0, age - 55) + (0.10 if "t2dm" in conds else 0) + (0.05 if "htn" in conds else 0)
        if rng.random() < ckd_p:
            conds.add("ckd")
        meds: list[str] = []
        if "t2dm" in conds and rng.random() < 0.7:
            meds.append("metformin")
        if "htn" in conds or "hf" in conds:
            if rng.random() < 0.65:
                meds.append(rng.choice(["lisinopril", "losartan"]))
            if rng.random() < 0.45:
                meds.append(rng.choice(["hctz", "furosemide"]))
        if "hf" in conds and rng.random() < 0.3:
            meds.append("spironolactone")
        if rng.random() < 0.12:
            meds.append(rng.choice(["ibuprofen", "naproxen"]))
        if "afib" in conds and rng.random() < 0.6:
            meds.append(rng.choice(["apixaban5", "rivaroxaban20"]))
        if rng.random() < 0.02:
            meds.append("lithium")
        meds = list(dict.fromkeys(meds))
        # creatinine: CKD raises it
        base = rng.gauss(0.95 if sex == "male" else 0.78, 0.15) + (0.006 * max(0, age - 50))
        if "ckd" in conds:
            base += rng.uniform(0.5, 1.4)
        creat = round(max(0.5, base), 2)
        lab_days = None if rng.random() < 0.12 else rng.choice([3, 10, 25, 45, 70, 85, 120, 200])
        resources: list[dict[str, Any]] = [
            {
                "resourceType": "Patient",
                "id": pid,
                "gender": sex,
                "birthDate": birth.isoformat(),
                "name": [{"text": f"Synthetic-{i:04d}"}],
                "meta": {"tag": [{"system": "urn:medsafe:data", "code": "synthea-style-synthetic"}]},
            }
        ]
        for j, c in enumerate(sorted(conds)):
            resources.append(
                {
                    "resourceType": "Condition",
                    "id": f"{pid}-cond-{j}",
                    "subject": {"reference": f"Patient/{pid}"},
                    "clinicalStatus": {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                "code": "active",
                            }
                        ]
                    },
                    "code": {
                        "coding": [{"system": SCT, "code": _CONDS[c][0], "display": _CONDS[c][1]}],
                        "text": _CONDS[c][1],
                    },
                }
            )
        for j, m in enumerate(meds):
            resources.append(med_resource(pid, m, idx=j))
        if lab_days is not None:
            when = RECORD_DATE - timedelta(days=lab_days)
            resources.append(_obs(pid, "creatinine", "2160-0", "Creatinine", creat, "mg/dL", when))
            egfr = ckd_epi_2021(creat, (when - birth).days / 365.2425, sex)
            resources.append(_obs(pid, "egfr", "98979-8", "eGFR CKD-EPI 2021", egfr, "mL/min/1.73m2", when))
            if any(m in meds for m in ("lisinopril", "losartan", "spironolactone")) and rng.random() < 0.8:
                kwhen = RECORD_DATE - timedelta(days=rng.choice([2, 8, 20, 40, 90]))
                resources.append(
                    _obs(
                        pid,
                        "potassium",
                        "2823-3",
                        "Potassium",
                        round(rng.gauss(4.4, 0.5), 1),
                        "mmol/L",
                        kwhen,
                    )
                )
        if rng.random() < 0.85:
            wt = round(rng.gauss(88 if sex == "male" else 74, 16), 1)
            resources.append(
                _obs(
                    pid,
                    "weight",
                    "29463-7",
                    "Body weight",
                    max(38.0, wt),
                    "kg",
                    RECORD_DATE - timedelta(days=rng.choice([10, 60, 200, 500])),
                )
            )
        docs.append(
            {
                "id": pid,
                "label": f"{sex[0].upper()}, {age} y, {', '.join(sorted(conds)) or 'no chronic conditions'}",
                "synthetic": True,
                "record_date": RECORD_DATE.isoformat(),
                "resources": resources,
            }
        )
    return docs
