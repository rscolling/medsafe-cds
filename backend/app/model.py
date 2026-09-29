"""Domain model shared by both data sources and the rules engine.

Adapters return FHIR-shaped dicts; :func:`app.context.build_context` normalises them into a
:class:`PatientContext`. Rules only ever see this normalised view, which is what guarantees that
the same rule fires identically for FHIR- and VistA-sourced patients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

LabSource = Literal["reported", "computed"]


@dataclass(frozen=True)
class LabValue:
    when: date
    value: float
    unit: str
    source: LabSource = "reported"


@dataclass(frozen=True)
class DrugRef:
    """A drug (active med or draft order) after RxNorm ingredient mapping."""

    raw: str
    rxcui: str | None
    name: str
    classes: frozenset[str]
    strength_mg: float | None = None
    dose_mg: float | None = None  # single administered dose if known (draft orders)
    order_id: str | None = None

    @property
    def mapped(self) -> bool:
        return self.rxcui is not None


@dataclass
class PatientContext:
    patient_id: str
    source: str
    sex: str  # "male" | "female" | "unknown"
    birth_date: date | None
    as_of: date
    meds: list[DrugRef] = field(default_factory=list)
    conditions: dict[str, str] = field(default_factory=dict)  # key -> display
    labs: dict[str, list[LabValue]] = field(default_factory=dict)  # sorted ascending by date
    unmapped_meds: list[str] = field(default_factory=list)

    def age_years(self) -> float | None:
        if self.birth_date is None:
            return None
        return (self.as_of - self.birth_date).days / 365.2425

    def latest_lab(self, key: str, within_days: int | None = None) -> LabValue | None:
        """Most recent lab at or before ``as_of`` (optionally inside a relative window)."""
        candidates = [v for v in self.labs.get(key, []) if v.when <= self.as_of]
        if not candidates:
            return None
        latest = max(candidates, key=lambda v: v.when)
        if within_days is not None and (self.as_of - latest.when).days > within_days:
            return None
        return latest

    def newest_lab_any_age(self, key: str) -> LabValue | None:
        return self.latest_lab(key, None)


@dataclass(frozen=True)
class Alert:
    rule_id: str
    rule_version: str
    mode: str  # "baseline" | "context"
    indicator: str  # info | warning | critical
    summary: str
    detail: str
    why: tuple[str, ...]
    source_label: str
    source_url: str
    suggestions: tuple[str, ...]
    override_reasons: tuple[tuple[str, str], ...]
    data_gap: bool = False
    facts: dict[str, object] = field(default_factory=dict)
    suggestion_defs: tuple[object, ...] = ()


@dataclass(frozen=True)
class Suppressed:
    """Baseline would have fired but context says the alert is not warranted."""

    rule_id: str
    rule_version: str
    reason: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationResult:
    mode: str
    alerts: tuple[Alert, ...]
    suppressed: tuple[Suppressed, ...] = ()
