"""Display views of source data for the order-entry UI: patient identity (picker) and current medication list.

Framework-free and defensive: both adapters return FHIR R4-shaped dicts, but nothing here trusts their shape.
Only identifying fields go into :func:`patient_identity` (no medications, labs or scenario labels), so the patient
picker reads like an EHR patient list. All data in this repo is synthetic.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.adapters.vista.parsing import VISTA_STATUS_URL
from app.context import _d, _dicts, _num, drugs_from_codeable, parse_date
from app.mapping.drugs import RXNORM_SYSTEM, DrugMapper, default_mapper

Resource = dict[str, Any]
MRN_TYPE_SYSTEM = "http://terminology.hl7.org/CodeSystem/v2-0203"
_SEX = {"male": "M", "female": "F", "other": "O", "unknown": "U"}
_SIG_PREFIX = re.compile(r"^\s*sig:\s*", re.IGNORECASE)


def _name(patient: Resource) -> str | None:
    for n in _dicts(patient.get("name")):
        text = n.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        family = n.get("family") if isinstance(n.get("family"), str) else ""
        given = (
            " ".join(g for g in n.get("given", []) if isinstance(g, str)) if isinstance(n.get("given"), list) else ""
        )
        if family or given:
            return f"{family}, {given}".strip(", ")
    return None


def _mrn(patient: Resource) -> str | None:
    """Medical record number: the identifier typed ``MR`` (HL7 v2-0203), if the record carries one."""
    for ident in _dicts(patient.get("identifier")):
        for coding in _dicts(_d(ident.get("type")).get("coding")):
            if coding.get("code") == "MR" and coding.get("system") in (MRN_TYPE_SYSTEM, None):
                value = ident.get("value")
                if isinstance(value, str) and value:
                    return value
    return None


def age_on(birth: str | None, today: date) -> int | None:
    """Whole years on ``today``. A partial birthDate (year or year-month only) gives ``None``, never a guessed age."""
    if not birth or len(birth) < 10:
        return None
    born = parse_date(birth)
    if born is None or born > today:
        return None
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def patient_identity(patient: Resource | None, today: date) -> dict[str, Any]:
    """Name, sex, birth date, age and MRN from a Patient resource. ``demographics`` says whether it was available."""
    if not patient:
        return {"name": None, "sex": None, "birthDate": None, "age": None, "mrn": None, "demographics": "unavailable"}
    gender = patient.get("gender")
    birth = patient.get("birthDate") if isinstance(patient.get("birthDate"), str) else None
    return {
        "name": _name(patient),
        "sex": _SEX.get(gender, "U") if isinstance(gender, str) else None,
        "birthDate": birth,
        "age": age_on(birth, today),
        "mrn": _mrn(patient),
        "demographics": "ok",
    }


def _sig(med: Resource) -> str | None:
    parts: list[str] = []
    for di in _dicts(med.get("dosageInstruction")):
        text = di.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(_SIG_PREFIX.sub("", text).strip())
            continue
        for dr in _dicts(di.get("doseAndRate")):
            q = _d(dr.get("doseQuantity"))
            value, unit = _num(q.get("value")), q.get("unit")
            if value is not None:
                amount = f"{value:g}"
                parts.append(f"{amount} {unit}" if isinstance(unit, str) and unit else amount)
    return "; ".join(parts) or None


def medication_summary(med: Resource, mapper: DrugMapper | None = None) -> dict[str, Any]:
    """One current medication for display: name, sig/dose, status, category and RxNorm codes.

    RxNorm comes from the record's own coding when present (``rxnormSource: coded``). VistA ``ORWPS ACTIVE`` only
    has free text, so its codes are the ingredient RxCUIs found by the same mapper the rules use
    (``rxnormSource: mapped``), and ``[]`` when the text is not recognised.
    """
    concept = _d(med.get("medicationCodeableConcept"))
    codings = [c for c in _dicts(concept.get("coding")) if c.get("system") == RXNORM_SYSTEM and c.get("code")]
    text = concept.get("text")
    name = text if isinstance(text, str) and text.strip() else None
    if name is None:
        display = next((c.get("display") for c in codings if isinstance(c.get("display"), str)), None)
        name = display or "unnamed medication"
    rxnorm: list[dict[str, str]]
    if codings:
        source = "coded"
        rxnorm = [{"code": str(c["code"]), "display": str(c.get("display") or "")} for c in codings]
    else:
        source = "mapped"
        refs = drugs_from_codeable({"text": name}, mapper or default_mapper())
        rxnorm = [{"code": r.rxcui, "display": r.name} for r in refs if r.rxcui]
    category = next(
        (c.get("text") for c in _dicts(med.get("category")) if isinstance(c.get("text"), str) and c.get("text")), None
    )
    status = med.get("status")
    source_status = next(
        (
            e.get("valueString")
            for e in _dicts(med.get("extension"))
            if e.get("url") == VISTA_STATUS_URL and isinstance(e.get("valueString"), str)
        ),
        None,
    )
    return {
        "id": med.get("id") if isinstance(med.get("id"), str) else None,
        "name": name.strip(),
        "sig": _sig(med),
        "status": status if isinstance(status, str) else "unknown",
        "sourceStatus": source_status,  # the source's own status word (VistA: PENDING / ACTIVE / HOLD ...), if any
        "category": category,
        "rxnorm": rxnorm,
        "rxnormSource": source if rxnorm else None,
    }


__all__ = ["age_on", "medication_summary", "patient_identity"]
