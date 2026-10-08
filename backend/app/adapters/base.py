"""The single interface both data sources implement (FHIR-shaped return values)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Resource = dict[str, Any]


class SourceUnavailableError(RuntimeError):
    """The data source could not be reached (timeouts, refused connections, protocol errors)."""


@dataclass(frozen=True)
class PatientSummary:
    id: str
    label: str
    synthetic_kind: str  # e.g. "hand-authored", "vehu", "vehu-synthetic-overlay", "synthea"
    note: str = ""
    # The patient's own Patient resource (demographics) when the source could supply it cheaply, else None.
    # ``label`` is the internal scenario label (docs, tests); a patient picker shows identity from ``patient``.
    patient: Resource | None = field(default=None, compare=False, hash=False)


class PatientSource(Protocol):
    """Read-only patient data provider. All returns are FHIR R4-shaped dicts."""

    name: str

    def list_patients(self) -> list[PatientSummary]: ...

    def get_patient(self, patient_id: str) -> Resource: ...

    def get_active_meds(self, patient_id: str) -> list[Resource]:
        """MedicationRequest resources for current (active / pending / on-hold) medications."""
        ...

    def get_problems(self, patient_id: str) -> list[Resource]:
        """Condition resources (problem list)."""
        ...

    def get_lab_series(self, patient_id: str) -> list[Resource]:
        """Observation resources (labs + weight), any order."""
        ...

    def status(self) -> dict[str, Any]: ...
