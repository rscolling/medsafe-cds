"""Named Python check functions for logic YAML cannot express.

Each check receives the normalised patient context, the draft order, the full regimen (active meds
+ draft) and the rule's ``params``; it returns a :class:`CheckOutcome`. Checks are pure functions of
their inputs (``ctx.as_of`` carries "now"), which keeps them deterministic and unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.clinical.egfr import cockcroft_gault
from app.model import DrugRef, PatientContext

Status = Literal["fire", "quiet", "data_gap"]


@dataclass
class CheckOutcome:
    status: Status
    facts: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)  # why quiet / why data gap
    indicator: str | None = None  # optional override of the card indicator


CheckFn = Callable[[PatientContext, DrugRef, list[DrugRef], dict[str, Any]], CheckOutcome]
REGISTRY: dict[str, CheckFn] = {}


def check(name: str) -> Callable[[CheckFn], CheckFn]:
    def deco(fn: CheckFn) -> CheckFn:
        REGISTRY[name] = fn
        return fn

    return deco


def num(x: float | None) -> str:
    if x is None:
        return "unknown"
    return f"{x:g}"


def _egfr_facts(ctx: PatientContext, order: DrugRef, window: int) -> tuple[dict[str, Any], Any]:
    lab = ctx.latest_lab("egfr", window)
    facts: dict[str, Any] = {"order": order.raw, "window_days": window}
    if lab is not None:
        facts.update(
            egfr=num(lab.value),
            egfr_date=lab.when.isoformat(),
            egfr_source="reported" if lab.source == "reported" else "computed by CKD-EPI 2021 from creatinine",
        )
    return facts, lab


@check("metformin_egfr")
def metformin_egfr(ctx: PatientContext, order: DrugRef, regimen: list[DrugRef], params: dict[str, Any]) -> CheckOutcome:
    """Latest eGFR within N days: <30 contraindicated, <45 caution, else quiet, none -> data gap."""
    window = int(params["window_days"])
    facts, lab = _egfr_facts(ctx, order, window)
    if lab is None:
        return CheckOutcome("data_gap", facts, [f"no eGFR/creatinine within {window} days"])
    if lab.value < params["contraindicated_below"]:
        facts.update(
            threshold=num(params["contraindicated_below"]),
            threshold_text=f"eGFR < {params['contraindicated_below']}: contraindicated per labelling",
            label_text="Labelling: metformin is contraindicated at eGFR below 30 mL/min/1.73 m2.",
        )
        return CheckOutcome("fire", facts, indicator="critical")
    if lab.value < params["caution_below"]:
        facts.update(
            threshold=num(params["caution_below"]),
            threshold_text=f"eGFR {params['contraindicated_below']}-{params['caution_below']}: initiation not recommended",
            label_text=(
                "Labelling: initiation is not recommended at eGFR 30-45; assess benefit/risk of "
                "continuing if eGFR falls below 45."
            ),
        )
        return CheckOutcome("fire", facts)
    return CheckOutcome("quiet", facts, [f"eGFR {num(lab.value)} >= {params['caution_below']}"])


@check("triple_whammy_renal")
def triple_whammy_renal(
    ctx: PatientContext, order: DrugRef, regimen: list[DrugRef], params: dict[str, Any]
) -> CheckOutcome:
    """Fire only if the latest eGFR (within N days) is below the threshold."""
    window = int(params["window_days"])
    facts, lab = _egfr_facts(ctx, order, window)
    facts.update(egfr_below=num(params["egfr_below"]))
    if lab is None:
        return CheckOutcome("data_gap", facts, [f"no eGFR/creatinine within {window} days"])
    if lab.value < params["egfr_below"]:
        return CheckOutcome("fire", facts)
    return CheckOutcome("quiet", facts, [f"eGFR {num(lab.value)} >= {params['egfr_below']}"])


def _dose(order: DrugRef) -> float | None:
    return order.dose_mg if order.dose_mg is not None else order.strength_mg


@check("apixaban_dose")
def apixaban_dose(ctx: PatientContext, order: DrugRef, regimen: list[DrugRef], params: dict[str, Any]) -> CheckOutcome:
    """Apixaban 5 mg BID in NVAF with >=2 of: age>=80, weight<=60 kg, creatinine>=1.5 mg/dL."""
    dose = _dose(order)
    facts: dict[str, Any] = {"order": order.raw, "dose": num(dose)}
    if dose is None:
        return CheckOutcome(
            "data_gap",
            {**facts, "criteria_text": "dose unknown", "missing_text": "order dose"},
            ["dose unknown"],
        )
    if dose <= 2.5:
        return CheckOutcome("quiet", facts, ["dose already 2.5 mg"])
    if "afib" not in ctx.conditions:
        return CheckOutcome(
            "quiet",
            facts,
            ["no atrial fibrillation on the problem list; NVAF criteria not applicable"],
        )

    met: list[str] = []
    known_not_met: list[str] = []
    missing: list[str] = []
    age = ctx.age_years()
    if age is None:
        missing.append("age")
    elif age >= params["age_gte"]:
        met.append(f"age {age:.0f} >= {params['age_gte']}")
    else:
        known_not_met.append(f"age {age:.0f}")
    wt = ctx.latest_lab("weight", params["weight_window_days"])
    if wt is None:
        missing.append(f"weight (none within {params['weight_window_days']} days)")
    elif wt.value <= params["weight_kg_lte"]:
        met.append(f"weight {num(wt.value)} kg <= {params['weight_kg_lte']} ({wt.when})")
    else:
        known_not_met.append(f"weight {num(wt.value)} kg")
    cr = ctx.latest_lab("creatinine", params["creatinine_window_days"])
    if cr is None:
        missing.append(f"serum creatinine (none within {params['creatinine_window_days']} days)")
    elif cr.value >= params["creatinine_mg_dl_gte"]:
        met.append(f"creatinine {num(cr.value)} mg/dL >= {params['creatinine_mg_dl_gte']} ({cr.when})")
    else:
        known_not_met.append(f"creatinine {num(cr.value)} mg/dL")

    facts.update(
        criteria_met=str(len(met)),
        criteria_text="; ".join(met) if met else "none",
        missing_text="; ".join(missing) if missing else "none",
    )
    needed = int(params["criteria_needed"])
    if len(met) >= needed:
        return CheckOutcome("fire", facts)
    if len(met) + len(missing) >= needed:
        facts["criteria_text"] = "; ".join([*met, *known_not_met]) or "none"
        return CheckOutcome("data_gap", facts, [f"missing: {facts['missing_text']}"])
    return CheckOutcome("quiet", facts, [f"only {len(met)} of 3 dose-reduction criteria can be met"])


@check("rivaroxaban_renal")
def rivaroxaban_renal(
    ctx: PatientContext, order: DrugRef, regimen: list[DrugRef], params: dict[str, Any]
) -> CheckOutcome:
    """Rivaroxaban >15 mg daily in NVAF when Cockcroft-Gault CrCl <= 50 mL/min."""
    dose = _dose(order)
    facts: dict[str, Any] = {"order": order.raw, "dose": num(dose), "extra": ""}
    if dose is None:
        return CheckOutcome("data_gap", {**facts, "missing_text": "order dose"}, ["dose unknown"])
    if dose <= 15:
        return CheckOutcome("quiet", facts, ["dose already 15 mg or lower"])
    if "afib" not in ctx.conditions:
        return CheckOutcome(
            "quiet",
            facts,
            ["no atrial fibrillation on the problem list; NVAF dose rule not applicable"],
        )
    age = ctx.age_years()
    cr = ctx.latest_lab("creatinine", params["crcl_window_days"])
    wt = ctx.latest_lab("weight", params["weight_window_days"])
    missing = [n for n, v in (("age", age), ("serum creatinine", cr), ("weight", wt)) if v is None]
    if ctx.sex == "unknown":
        missing.append("sex")
    if missing or age is None or cr is None or wt is None:
        facts["missing_text"] = ", ".join(missing)
        return CheckOutcome("data_gap", facts, [f"missing: {facts['missing_text']}"])
    crcl = cockcroft_gault(cr.value, age, wt.value, ctx.sex)
    facts.update(
        crcl=num(crcl),
        age=age,
        weight=num(wt.value),
        weight_date=wt.when.isoformat(),
        creatinine=num(cr.value),
        creatinine_date=cr.when.isoformat(),
    )
    if crcl < params["avoid_below"]:
        facts["extra"] = "At CrCl below 15 mL/min, review the label's renal-impairment guidance."
    if crcl <= params["reduce_below"]:
        return CheckOutcome("fire", facts)
    return CheckOutcome("quiet", facts, [f"CrCl {num(crcl)} mL/min > {params['reduce_below']}"])
