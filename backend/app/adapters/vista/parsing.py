"""Parsers for raw RPC Broker replies -> FHIR-shaped resources.

Formats were observed live against ``worldvista/vehu`` (see docs/integration-notes.md).
Replies are CRLF-separated lines; fields are ``^``-separated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.adapters.vista.fileman import fileman_to_date, fileman_to_datetime
from app.mapping import codes
from app.mapping.drugs import parse_strength_mg

Resource = dict[str, Any]

# ORWPS ACTIVE statuses that mean "the patient currently has this medication".
# (VEHU: outpatient orders are all PENDING, non-VA meds are ACTIVE.)
CURRENT_STATUS = {
    "ACTIVE": "active",
    "PENDING": "active",
    "HOLD": "on-hold",
    "PROVIDER HOLD": "on-hold",
    "SUSPENDED": "active",
    "ACTIVE/SUSP": "active",
    "REFILL": "active",
}
ORDER_TYPES = {"OP": "outpatient", "NV": "non-VA", "UD": "unit dose", "IV": "IV", "CP": "clinic"}


def lines(raw: str) -> list[str]:
    return raw.replace("\r\n", "\n").split("\n")


# ---------------------------------------------------------------- patient
def parse_patient_select(dfn: str, raw: str) -> Resource:
    """ORWPT SELECT -> ``NAME^SEX^DOB(FileMan)^SSN^...``. We deliberately drop SSN (fake in VEHU)."""
    parts = raw.strip().split("\r\n")[0].split("^")
    if len(parts) < 3 or not parts[0]:
        raise ValueError(f"unexpected ORWPT SELECT reply for {dfn}: {raw[:80]!r}")
    dob = fileman_to_date(parts[2])
    res: Resource = {
        "resourceType": "Patient",
        "id": dfn,
        "identifier": [{"system": "urn:vista:vehu:dfn", "value": dfn}],
        "name": [{"text": parts[0]}],
        "gender": {"M": "male", "F": "female"}.get(parts[1].strip(), "unknown"),
        "meta": {"tag": [{"system": "urn:medsafe:data", "code": "vehu-synthetic"}]},
    }
    if dob:
        res["birthDate"] = dob.isoformat()
    return res


# ---------------------------------------------------------------- meds
@dataclass
class VistaMed:
    order_type: str
    order_id: str
    name: str
    status: str
    sig: str = ""
    extra: list[str] = field(default_factory=list)


def parse_active_meds(raw: str) -> list[VistaMed]:
    """ORWPS ACTIVE. Header ``~TYPE^ORDERID^NAME^...^ORDERNUM^STATUS``; then name / ``\\`` sig lines."""
    meds: list[VistaMed] = []
    for line in lines(raw):
        if line.startswith("~"):
            f = line[1:].split("^")
            if len(f) < 3:
                continue
            meds.append(
                VistaMed(
                    order_type=f[0][:2],
                    order_id=f[1],
                    name=f[2].strip(),
                    status=(f[9].strip().upper() if len(f) > 9 else ""),
                )
            )
        elif meds and line.strip():
            text = line.strip()
            if text.startswith("\\"):
                meds[-1].sig = (meds[-1].sig + " " + text.lstrip("\\ ").strip()).strip()
            else:
                meds[-1].extra.append(text)
    return meds


def med_to_medication_request(dfn: str, med: VistaMed) -> Resource | None:
    status = CURRENT_STATUS.get(med.status)
    if status is None:
        return None
    res: Resource = {
        "resourceType": "MedicationRequest",
        "id": f"{dfn}-{med.order_id}",
        "status": status,
        "intent": "order",
        "subject": {"reference": f"Patient/{dfn}"},
        "medicationCodeableConcept": {"text": med.name},  # free text: mapped downstream to RxNorm
        "category": [{"text": ORDER_TYPES.get(med.order_type, med.order_type)}],
    }
    if med.sig:
        res["dosageInstruction"] = [{"text": med.sig}]
    strength = parse_strength_mg(med.name)
    if strength is not None:
        res["extension"] = [{"url": "urn:medsafe:strength-mg-from-name", "valueDecimal": strength}]
    return res


# ---------------------------------------------------------------- problems
_SCT = re.compile(r"\(SCT\s+(\d+)\)\s*$")


def parse_problems(dfn: str, raw: str) -> list[Resource]:
    """ORQQPL LIST -> ``IEN^text (SCT code)^A^ICD^onset...``."""
    out: list[Resource] = []
    for line in lines(raw):
        f = line.split("^")
        if len(f) < 4 or not f[0].strip().isdigit() or f[2].strip() not in {"A", ""}:
            continue  # header row "^No problems found." etc.
        text = f[1].strip()
        codings: list[dict[str, str]] = []
        m = _SCT.search(text)
        if m:
            display = _SCT.sub("", text).strip()
            codings.append({"system": codes.SNOMED, "code": m.group(1), "display": display})
        icd = f[3].strip()
        if icd:
            codings.append({"system": codes.ICD9CM, "code": icd, "display": text})
        res: Resource = {
            "resourceType": "Condition",
            "id": f"{dfn}-{f[0].strip()}",
            "clinicalStatus": {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                        "code": "active",
                    }
                ]
            },
            "code": {"coding": codings, "text": _SCT.sub("", text).strip()},
            "subject": {"reference": f"Patient/{dfn}"},
        }
        onset = fileman_to_date(f[5]) if len(f) > 5 else None
        if onset:
            res["onsetDateTime"] = onset.isoformat()
        out.append(res)
    return out


# ---------------------------------------------------------------- labs
@dataclass
class LabSet:
    collected: str  # FileMan
    results: list[dict[str, str]]


def parse_lab_set(raw: str) -> LabSet | None:
    """ORWLRR INTERIMG (format 1): one collection per call.

    Line 1 ``?^CH^collectFM^specIEN^SPECIMEN^accession^provider...``;
    result lines ``testIEN^NAME^VALUE^FLAG^UNITS^REFRANGE``; then free-text footer.
    """
    if not raw.strip():
        return None
    ls = lines(raw)
    head = ls[0].split("^")
    if len(head) < 3:
        return None
    results: list[dict[str, str]] = []
    for line in ls[1:]:
        f = line.split("^")
        if len(f) >= 6 and f[0].strip().isdigit():
            results.append(
                {
                    "test_ien": f[0].strip(),
                    "name": f[1].strip(),
                    "value": f[2].strip(),
                    "flag": f[3].strip(),
                    "units": f[4].strip(),
                    "range": f[5].strip(),
                }
            )
    return LabSet(collected=head[2], results=results)


def lab_sets_to_observations(dfn: str, sets: list[LabSet]) -> list[Resource]:
    """Keep only the tests we map (creatinine, potassium, lithium); others are dropped."""
    tests = codes.vista_lab_tests()
    out: list[Resource] = []
    for s in sets:
        when = fileman_to_datetime(s.collected)
        if when is None:
            continue
        for r in s.results:
            key = tests.get(r["name"].upper())
            if key is None:
                continue
            try:
                value = float(r["value"].replace("<", "").replace(">", ""))
            except ValueError:
                continue
            loinc = codes.PRIMARY_LOINC[key]
            out.append(
                {
                    "resourceType": "Observation",
                    "id": f"{dfn}-{s.collected}-{r['test_ien']}",
                    "status": "final",
                    "code": {
                        "coding": [{"system": codes.LOINC, "code": loinc}],
                        "text": r["name"],
                    },
                    "subject": {"reference": f"Patient/{dfn}"},
                    "effectiveDateTime": when.isoformat(timespec="minutes"),
                    "valueQuantity": {
                        "value": value,
                        "unit": r["units"],
                        "system": codes.UCUM,
                    },
                }
            )
    return out


def parse_weight(dfn: str, raw: str) -> list[Resource]:
    """ORQQVI VITALS -> ``id^TYPE^value^FMdate^display^metric``; keep weight (WT) in kg."""
    out: list[Resource] = []
    for line in lines(raw):
        f = line.split("^")
        if len(f) < 5 or f[1] != "WT":
            continue
        when = fileman_to_datetime(f[3])
        if when is None:
            continue
        kg: float | None = None
        m = re.search(r"\(([\d.]+)\s*kg\)", f[5] if len(f) > 5 else "")
        if m:
            kg = float(m.group(1))
        else:
            try:
                kg = round(float(f[2]) * 0.45359237, 2)  # VistA stores lb
            except ValueError:
                continue
        out.append(
            {
                "resourceType": "Observation",
                "id": f"{dfn}-wt-{f[3]}",
                "status": "final",
                "code": {"coding": [{"system": codes.LOINC, "code": "29463-7"}], "text": "Weight"},
                "subject": {"reference": f"Patient/{dfn}"},
                "effectiveDateTime": when.isoformat(timespec="minutes"),
                "valueQuantity": {"value": kg, "unit": "kg", "system": codes.UCUM},
            }
        )
    return out


def newest_oldest(raw: str) -> tuple[date | None, date | None]:
    """ORWLRR NEWOLD -> ``newestFM^oldestFM`` (``^`` alone = no labs)."""
    a, _, b = raw.strip().partition("^")
    return fileman_to_date(a), fileman_to_date(b)
