"""Application service: data source -> normalised context -> rules -> alerts. Framework-free."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.adapters.base import PatientSource, SourceUnavailableError
from app.context import build_context, orders_from_resource
from app.logging_setup import short_hash
from app.mapping.drugs import clinical_drugs, default_mapper
from app.model import DrugRef, EvaluationResult, PatientContext
from app.rules.engine import RulesEngine

logger = logging.getLogger("medsafe.service")
Resource = dict[str, Any]


class UnknownSourceError(KeyError):
    pass


@dataclass
class Evaluated:
    ctx: PatientContext
    order: DrugRef
    result: EvaluationResult
    # Set when no check could be performed for the draft (unidentified / non-systemic / partly recognised drug).
    unchecked_reason: str | None = None
    orders: tuple[DrugRef, ...] = ()


class CdsService:
    def __init__(self, engine: RulesEngine, sources: dict[str, PatientSource], as_of_policy: str = "anchored") -> None:
        self.engine = engine
        self.sources = sources
        self.as_of_policy = as_of_policy  # default; per-source overrides in ``as_of_policy_by_source``
        self.as_of_policy_by_source: dict[str, str] = {}

    def policy_for(self, source: str) -> str:
        return self.as_of_policy_by_source.get(source, self.as_of_policy)

    def source(self, name: str) -> PatientSource:
        try:
            return self.sources[name]
        except KeyError as exc:
            raise UnknownSourceError(name) from exc

    def load_context(self, source: str, patient_id: str, as_of: date | None = None) -> PatientContext:
        src = self.source(source)
        return build_context(
            src.get_patient(patient_id),
            src.get_active_meds(patient_id),
            src.get_problems(patient_id),
            src.get_lab_series(patient_id),
            source=source,
            as_of=as_of,
            as_of_policy=self.policy_for(source),
        )

    def evaluate(self, source: str, patient_id: str, draft: Resource, mode: str) -> Evaluated:
        ctx = self.load_context(source, patient_id)
        return self.evaluate_ctx(ctx, draft, mode)

    def evaluate_ctx(self, ctx: PatientContext, draft: Resource, mode: str) -> Evaluated:
        """Evaluate a draft order. A combination product is expanded to one drug per ingredient.

        Every ingredient is evaluated against the regimen that contains *all* of them (so a draft of
        ``LISINOPRIL-HCTZ`` plus current ibuprofen sees lisinopril, HCTZ and ibuprofen together). Alerts are
        merged, one per rule. A draft that cannot be checked is reported via ``unchecked_reason`` instead of
        silently passing.
        """
        orders = orders_from_resource(draft, default_mapper())
        mapped = [o for o in orders if o.mapped]
        if not mapped:
            reason = _unchecked_reason(orders[0])
            logger.warning(
                "draft order could not be checked",
                extra={"order_fingerprint": short_hash(orders[0].raw), "order_len": len(orders[0].raw), "why": reason},
            )
            return Evaluated(ctx, orders[0], EvaluationResult(mode, ()), unchecked_reason=reason, orders=tuple(orders))
        extra_ctx = ctx if len(mapped) == 1 else _with_sibling_drugs(ctx, mapped)
        alerts: dict[str, Any] = {}
        suppressed: dict[str, Any] = {}
        for order in mapped:
            result = self.engine.evaluate(extra_ctx, order, mode)
            for a in result.alerts:
                alerts.setdefault(a.rule_id, a)
            for s in result.suppressed:
                suppressed.setdefault(s.rule_id, s)
        for rule_id in alerts:
            suppressed.pop(rule_id, None)
        merged = EvaluationResult(mode, tuple(alerts.values()), tuple(suppressed.values()))
        partial = next((o for o in mapped if o.partial), None)
        reason = (
            "only part of this combination product was recognised, so checks for its other ingredients were not run"
            if partial
            else None
        )
        if reason:
            logger.warning("draft order only partly recognised", extra={"order_fingerprint": short_hash(mapped[0].raw)})
        return Evaluated(ctx, mapped[0], merged, unchecked_reason=reason, orders=tuple(mapped))


def _unchecked_reason(order: DrugRef) -> str:
    from app.mapping.drugs import default_mapper as mapper

    excluded = mapper().map_text(order.raw).excluded if order.raw else ""
    if excluded:
        return f"{excluded}: not a systemic drug order, no interaction or renal check performed"
    return "the drug could not be identified, so no medication-safety check was performed"


def _with_sibling_drugs(ctx: PatientContext, orders: list[DrugRef]) -> PatientContext:
    """Copy of ``ctx`` whose regimen also contains the other ingredients of a combination draft."""
    import copy

    twin = copy.copy(ctx)
    twin.meds = [*ctx.meds, *orders]  # the engine appends the order itself; the same object is never used twice
    return twin


def orderable_drugs() -> list[dict[str, str]]:
    """Drugs offered by the mock order-entry UI: coded clinical drugs (RxNorm SCD)."""
    return [
        {"rxcui": rx, "display": desc, "ingredient_rxcui": ing}
        for rx, (ing, _mg, desc) in sorted(clinical_drugs().items(), key=lambda kv: kv[1][2])
    ]


def draft_order(patient_id: str, rxcui: str, display: str | None = None) -> Resource:
    """Build a draft MedicationRequest from an SCD RxCUI (dose from the SCD strength)."""
    ing, strength, desc = clinical_drugs()[rxcui]
    res: Resource = {
        "resourceType": "MedicationRequest",
        "id": "draft-1",
        "status": "draft",
        "intent": "order",
        "subject": {"reference": f"Patient/{patient_id}"},
        "medicationCodeableConcept": {
            "coding": [
                {
                    "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                    "code": rxcui,
                    "display": desc,
                }
            ],
            "text": display or desc,
        },
    }
    if strength is not None and ing:
        res["dosageInstruction"] = [{"doseAndRate": [{"doseQuantity": {"value": strength, "unit": "mg"}}]}]
    return res


__all__ = [
    "CdsService",
    "Evaluated",
    "SourceUnavailableError",
    "UnknownSourceError",
    "draft_order",
    "orderable_drugs",
]
