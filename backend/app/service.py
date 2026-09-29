"""Application service: data source -> normalised context -> rules -> alerts. Framework-free."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.adapters.base import PatientSource, SourceUnavailableError
from app.context import build_context, order_from_resource
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


class CdsService:
    def __init__(self, engine: RulesEngine, sources: dict[str, PatientSource], as_of_policy: str = "anchored") -> None:
        self.engine = engine
        self.sources = sources
        self.as_of_policy = as_of_policy

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
            as_of_policy=self.as_of_policy,
        )

    def evaluate(self, source: str, patient_id: str, draft: Resource, mode: str) -> Evaluated:
        ctx = self.load_context(source, patient_id)
        return self.evaluate_ctx(ctx, draft, mode)

    def evaluate_ctx(self, ctx: PatientContext, draft: Resource, mode: str) -> Evaluated:
        order = order_from_resource(draft, default_mapper())
        if not order.mapped:
            logger.warning("draft order could not be mapped to an ingredient", extra={"order": order.raw})
            return Evaluated(ctx, order, EvaluationResult(mode, ()))
        return Evaluated(ctx, order, self.engine.evaluate(ctx, order, mode))


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
