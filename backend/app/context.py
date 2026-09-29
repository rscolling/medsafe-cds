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


def parse_datetime(value: Any) -> datetime | None:
    """Naive datetime of an ISO date/dateTime (timezone dropped: comparisons are within one record)."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        d = parse_date(value)
        return datetime(d.year, d.month, d.day) if d else None
    return dt.replace(tzinfo=None)


def drugs_from_codeable(
    concept: Resource | None,
    mapper: DrugMapper | None = None,
    *,
    dose_mg: float | None = None,
    order_id: str | None = None,
) -> list[DrugRef]:
    """Map a CodeableConcept to one :class:`DrugRef` per ingredient (combination products expand).

    A coded RxNorm SCD/ingredient yields one drug. Free text is tokenised; ``LISINOPRIL-HCTZ 20-12.5`` yields
    lisinopril (20 mg) and hydrochlorothiazide (12.5 mg). Products flagged non-systemic (gel, ophthalmic,
    flush, ...) yield no drug, and partly recognised combinations are marked ``partial``. An unmappable
    concept yields a single unmapped :class:`DrugRef` so callers can report it.
    """
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
    if rxcui is not None:
        ing = mapper.ingredient(rxcui)
        if ing is None:  # pragma: no cover - rxcui came from this same mapper
            raise ValueError(f"unknown RxCUI {rxcui}")
        return [
            DrugRef(
                raw=text or ing.name,
                rxcui=rxcui,
                name=ing.name,
                classes=ing.classes,
                strength_mg=strength,
                dose_mg=dose_mg if dose_mg is not None else strength,
                order_id=order_id,
            )
        ]
    if text:
        match = mapper.map_text(text)
        if match.excluded:
            return [DrugRef(raw=text, rxcui=None, name=text, classes=frozenset(), order_id=order_id, partial=False)]
        if match.ingredients:
            many = len(match.ingredients) > 1
            return [
                DrugRef(
                    raw=text,
                    rxcui=ing.rxcui,
                    name=ing.name,
                    classes=ing.classes,
                    strength_mg=mg,
                    # a single dose from the order applies to a single-ingredient product only
                    dose_mg=(dose_mg if (dose_mg is not None and not many) else mg),
                    order_id=order_id,
                    partial=match.partial,
                )
                for ing, mg in zip(
                    match.ingredients, match.strengths_mg or (None,) * len(match.ingredients), strict=False
                )
            ]
    return [DrugRef(raw=text, rxcui=None, name=text or "unknown", classes=frozenset(), order_id=order_id)]


def drug_from_codeable(
    concept: Resource | None,
    mapper: DrugMapper | None = None,
    *,
    dose_mg: float | None = None,
    order_id: str | None = None,
) -> DrugRef:
    """Map a CodeableConcept to a single DrugRef (the first ingredient; use :func:`drugs_from_codeable` for combos)."""
    return drugs_from_codeable(concept, mapper, dose_mg=dose_mg, order_id=order_id)[0]


def _dose_mg(med_request: Resource) -> float | None:
    for di in _dicts(med_request.get("dosageInstruction")):
        for dr in _dicts(di.get("doseAndRate")):
            q = _d(dr.get("doseQuantity"))
            if q.get("unit") in {"mg", "MG"}:
                value = _num(q.get("value"))
                if value is not None:
                    return value
    return None


def _order_id(med_request: Resource) -> str | None:
    value = med_request.get("id")
    return str(value) if isinstance(value, str | int) and not isinstance(value, bool) else None


def orders_from_resource(med_request: Resource, mapper: DrugMapper | None = None) -> list[DrugRef]:
    """One DrugRef per ingredient of a draft order (combination products expand)."""
    return drugs_from_codeable(
        med_request.get("medicationCodeableConcept"),
        mapper,
        dose_mg=_dose_mg(med_request),
        order_id=_order_id(med_request),
    )


def order_from_resource(med_request: Resource, mapper: DrugMapper | None = None) -> DrugRef:
    return orders_from_resource(med_request, mapper)[0]


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


def _observation_lab(obs: Resource, dropped: dict[str, int] | None = None) -> tuple[str, LabValue] | None:
    """Normalise one Observation to (lab key, LabValue) in the key's standard unit, or None (counted in ``dropped``)."""

    def drop(reason: str) -> None:
        if dropped is not None:
            dropped[reason] = dropped.get(reason, 0) + 1

    q = _d(obs.get("valueQuantity"))
    at = parse_datetime(obs.get("effectiveDateTime"))
    number = _num(q.get("value"))
    if number is None or at is None:
        return None
    for coding in _dicts(_d(obs.get("code")).get("coding")):
        loinc = coding.get("code")
        if coding.get("system") == codes.LOINC and isinstance(loinc, str) and loinc in codes.LAB_KEY_BY_LOINC:
            key = codes.LAB_KEY_BY_LOINC[loinc]
            raw_unit = q.get("unit") or q.get("code") or ""
            unit = raw_unit if isinstance(raw_unit, str) else ""
            value = number
            table = codes.UNIT_CONVERSIONS.get(key)
            if table is not None:
                factor = table.get(unit.strip().lower())
                if factor is None:
                    if unit.strip() == "" and key in {"creatinine", "potassium", "lithium"}:
                        # no unit at all: cannot tell mg/dL from umol/L, so do not guess
                        drop(f"{key}: missing unit")
                    else:
                        drop(f"{key}: unsupported unit {unit[:12]!r}")
                    return None
                value, unit = round(value * factor, 4), codes.NORMAL_UNIT[key]
            lo, hi = codes.PLAUSIBLE.get(key, (float("-inf"), float("inf")))
            if not lo <= value <= hi:
                drop(f"{key}: implausible value")
                return None
            if q.get("comparator") in {"<", ">", "<=", ">="}:
                # "<0.3" / ">20" is not an exact measurement; a rule must not treat the bound as the value
                drop(f"{key}: inexact ({q.get('comparator')})")
                return None
            return key, LabValue(at.date(), value, unit, "reported", at)
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
    today: date | None = None,
) -> PatientContext:
    """Build the normalised context.

    ``as_of_policy``:
      * ``anchored`` (default for the prototype): "now" is the date of the newest observation in the
        patient's own record. Needed for VistA/VEHU (data dated 2009-2016) and makes fixtures
        deterministic. Windows such as "eGFR within 90 days" are therefore relative to the record.
      * ``today``: wall-clock date (what a production deployment would use). A creatinine from years ago
        is then correctly *outside* every window, so the rule raises a data-gap card instead of a critical alert.
    """
    mapper = mapper or default_mapper()
    if not isinstance(patient, dict):
        raise ValueError("patient resource must be an object")
    medications, conditions, observations = _dicts(medications), _dicts(conditions), _dicts(observations)
    labs: dict[str, list[LabValue]] = {}
    dropped: dict[str, int] = {}
    newest: date | None = None
    for obs in observations:
        parsed = _observation_lab(obs, dropped)
        if parsed:
            labs.setdefault(parsed[0], []).append(parsed[1])
            if newest is None or parsed[1].when > newest:
                newest = parsed[1].when
    for series in labs.values():
        series.sort(key=lambda v: v.sort_key)

    if as_of is not None:
        policy = "explicit"
    elif as_of_policy == "anchored" and newest:
        as_of, policy = newest, "anchored"
    else:
        as_of, policy = today or datetime.now(UTC).date(), "today"
    if dropped:
        logger.info("observations not used", extra={"dropped": dict(dropped)})

    birth = parse_date(patient.get("birthDate"))
    gender = patient.get("gender", "unknown")
    sex = gender if isinstance(gender, str) and gender in {"male", "female"} else "unknown"

    # Compute eGFR (CKD-EPI 2021) from every creatinine that has no reported eGFR on the same day, so a stale
    # reported eGFR never hides a newer creatinine. A reported value on the same day always wins.
    if "creatinine" in labs and birth and sex != "unknown":
        reported_days = {v.when for v in labs.get("egfr", [])}
        computed: list[LabValue] = []
        for cr in labs["creatinine"]:
            if cr.when in reported_days:
                continue
            age = (cr.when - birth).days / 365.2425
            try:
                value = ckd_epi_2021(cr.value, age, sex)
            except ValueError:
                # e.g. a lab dated before the DOB: skip this one result, never abort the whole context
                dropped["egfr: not computable (age/creatinine)"] = (
                    dropped.get("egfr: not computable (age/creatinine)", 0) + 1
                )
                continue
            computed.append(LabValue(cr.when, value, "mL/min/1.73m2", "computed", cr.at))
        if computed:
            labs["egfr"] = sorted([*labs.get("egfr", []), *computed], key=lambda v: v.sort_key)

    ctx = PatientContext(
        patient_id=str(patient.get("id", ""))[:64],
        source=source,
        sex=sex,
        birth_date=birth,
        as_of=as_of,
        labs=labs,
        as_of_policy=policy,
        dropped_labs=dropped,
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
        text = _d(concept).get("text")
        for drug in drugs_from_codeable(concept, mapper, order_id=med_id if isinstance(med_id, str) else None):
            if drug.mapped:
                ctx.meds.append(drug)
                if drug.partial and drug.raw not in ctx.partial_meds:
                    ctx.partial_meds.append(drug.raw)
            elif isinstance(text, str) and mapper.map_text(text).excluded:
                ctx.excluded_meds.append(drug.raw)
            else:
                ctx.unmapped_meds.append(drug.raw)
    return ctx
