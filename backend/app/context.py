"""Normalise FHIR-shaped resources (from either adapter) into a :class:`PatientContext`."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from app.clinical.egfr import ckd_epi_2021
from app.mapping import codes
from app.mapping.drugs import RXNORM_SYSTEM, DrugMapper, clinical_drugs, default_mapper
from app.model import DrugRef, LabValue, PatientContext

logger = logging.getLogger("medsafe.context")

Resource = dict[str, Any]


def _d(value: Any) -> Resource:
    """Defensive: request payloads are untrusted, so anything that is not an object becomes ``{}``."""
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[Resource]:
    """Defensive: the object items of a list (anything else is ignored)."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None  # noqa: PLR0124 - NaN check


def parse_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def drug_from_codeable(
    concept: Resource | None,
    mapper: DrugMapper | None = None,
    *,
    dose_mg: float | None = None,
    order_id: str | None = None,
) -> DrugRef:
    """Map a FHIR CodeableConcept (RxNorm coding preferred, free text fallback) to a DrugRef."""
    mapper = mapper or default_mapper()
    concept = _d(concept)
    raw_text = concept.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    strength: float | None = None
    rxcui: str | None = None
    for coding in _dicts(concept.get("coding")):
        if coding.get("system") != RXNORM_SYSTEM:
            continue
        code = str(coding.get("code", ""))
        display = coding.get("display")
        text = text or (display if isinstance(display, str) else "")
        scd = clinical_drugs().get(code)
        if scd:
            rxcui, strength = scd[0], scd[1]
        elif mapper.ingredient(code):
            rxcui = code
        if rxcui:
            break
    if rxcui is None and text:
        match = mapper.map_text(text)
        if match.ingredients:
            # combination products: keep the first ingredient as identity, union the classes
            first = match.ingredients[0]
            return DrugRef(
                raw=text,
                rxcui=first.rxcui,
                name="+".join(i.name for i in match.ingredients),
                classes=match.classes,
                strength_mg=match.strength_mg,
                dose_mg=dose_mg if dose_mg is not None else match.strength_mg,
                order_id=order_id,
            )
    if rxcui is not None:
        ing = mapper.ingredient(rxcui)
        if ing is None:  # pragma: no cover - rxcui came from this same mapper
            raise ValueError(f"unknown RxCUI {rxcui}")
        return DrugRef(
            raw=text or ing.name,
            rxcui=rxcui,
            name=ing.name,
            classes=ing.classes,
            strength_mg=strength,
            dose_mg=dose_mg if dose_mg is not None else strength,
            order_id=order_id,
        )
    return DrugRef(raw=text, rxcui=None, name=text or "unknown", classes=frozenset(), order_id=order_id)


def _dose_mg(med_request: Resource) -> float | None:
    for di in _dicts(med_request.get("dosageInstruction")):
        for dr in _dicts(di.get("doseAndRate")):
            q = _d(dr.get("doseQuantity"))
            if q.get("unit") in {"mg", "MG"}:
                value = _num(q.get("value"))
                if value is not None:
                    return value
    return None


def order_from_resource(med_request: Resource, mapper: DrugMapper | None = None) -> DrugRef:
    return drug_from_codeable(
        med_request.get("medicationCodeableConcept"),
        mapper,
        dose_mg=_dose_mg(med_request),
        order_id=str(med_request["id"]) if isinstance(med_request.get("id"), str | int) else None,
    )


def _condition_key(cond: Resource) -> tuple[str, str] | None:
    code_cc = _d(cond.get("code"))
    for coding in _dicts(code_cc.get("coding")):
        system, code = coding.get("system"), str(coding.get("code", ""))
        key = None
        if system == codes.SNOMED:
            key = codes.condition_key_from_snomed(code)
        elif system == codes.ICD9CM:
            key = codes.condition_key_from_icd9(code)
        if key:
            label = coding.get("display") or code_cc.get("text") or key
            return key, label if isinstance(label, str) else key
    return None


def _is_active(res: Resource) -> bool:
    codings = _dicts(_d(res.get("clinicalStatus")).get("coding"))
    status = codings[0].get("code") if codings else None
    return status in (None, "active", "recurrence", "relapse")


def _observation_lab(obs: Resource) -> tuple[str, LabValue] | None:
    q = _d(obs.get("valueQuantity"))
    when = parse_date(obs.get("effectiveDateTime"))
    number = _num(q.get("value"))
    if number is None or when is None:
        return None
    for coding in _dicts(_d(obs.get("code")).get("coding")):
        loinc = coding.get("code")
        if coding.get("system") == codes.LOINC and isinstance(loinc, str) and loinc in codes.LAB_KEY_BY_LOINC:
            key = codes.LAB_KEY_BY_LOINC[loinc]
            value = number
            raw_unit = q.get("unit") or q.get("code") or ""
            unit = raw_unit if isinstance(raw_unit, str) else ""
            if key == "weight" and unit.lower() in {"lb", "[lb_av]", "lbs"}:
                value, unit = round(value * 0.45359237, 2), "kg"
            return key, LabValue(when, value, unit, "reported")
    return None


def build_context(
    patient: Resource,
    medications: list[Resource],
    conditions: list[Resource],
    observations: list[Resource],
    *,
    source: str,
    as_of: date | None = None,
    as_of_policy: str = "anchored",
    mapper: DrugMapper | None = None,
) -> PatientContext:
    """Build the normalised context.

    ``as_of_policy``:
      * ``anchored`` (default for the prototype): "now" is the date of the newest observation in the
        patient's own record. Needed for VistA/VEHU (data dated 2009-2016) and makes fixtures
        deterministic. Windows such as "eGFR within 90 days" are therefore relative to the record.
      * ``today``: wall-clock date (what a production deployment would use).
    """
    mapper = mapper or default_mapper()
    if not isinstance(patient, dict):
        raise ValueError("patient resource must be an object")
    medications, conditions, observations = _dicts(medications), _dicts(conditions), _dicts(observations)
    labs: dict[str, list[LabValue]] = {}
    newest: date | None = None
    for obs in observations:
        eff = parse_date(obs.get("effectiveDateTime"))
        if eff and (newest is None or eff > newest):
            newest = eff
        parsed = _observation_lab(obs)
        if parsed:
            labs.setdefault(parsed[0], []).append(parsed[1])
    for series in labs.values():
        series.sort(key=lambda v: v.when)

    if as_of is None:
        as_of = newest if (as_of_policy == "anchored" and newest) else datetime.now(UTC).date()

    birth = parse_date(patient.get("birthDate"))
    gender = patient.get("gender", "unknown")
    sex = gender if isinstance(gender, str) and gender in {"male", "female"} else "unknown"

    # Reported eGFR always wins; otherwise derive from creatinine (VistA has no eGFR at all).
    if "egfr" not in labs and "creatinine" in labs and birth and sex != "unknown":
        computed: list[LabValue] = []
        for cr in labs["creatinine"]:
            if cr.unit.lower() not in {"mg/dl", ""}:
                continue
            age = (cr.when - birth).days / 365.2425
            computed.append(LabValue(cr.when, ckd_epi_2021(cr.value, age, sex), "mL/min/1.73m2", "computed"))
        if computed:
            labs["egfr"] = computed

    ctx = PatientContext(
        patient_id=str(patient.get("id", ""))[:64],
        source=source,
        sex=sex,
        birth_date=birth,
        as_of=as_of,
        labs=labs,
    )
    for cond in conditions:
        if not _is_active(cond):
            continue
        found = _condition_key(cond)
        if found:
            ctx.conditions.setdefault(found[0], found[1])
    for med in medications:
        if med.get("status") not in (None, "active", "on-hold", "draft", "unknown"):
            continue
        concept = med.get("medicationCodeableConcept")
        med_id = med.get("id")
        drug = drug_from_codeable(concept, mapper, order_id=med_id if isinstance(med_id, str) else None)
        if drug.mapped:
            ctx.meds.append(drug)
        else:
            ctx.unmapped_meds.append(drug.raw)
    return ctx
