"""FHIR R4 data source: HAPI FHIR over HTTP (httpx) or the in-repo JSON fixtures.

``mode``:
  * ``http``     always talk to HAPI (``MEDSAFE_FHIR_BASE_URL``)
  * ``fixtures`` serve the in-repo patients (hand-authored + generated Synthea-style cohort)
  * ``auto``     use HAPI when it answers ``/metadata`` at startup, else fixtures (logged)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.adapters.base import PatientSummary, Resource, SourceUnavailableError

logger = logging.getLogger("medsafe.fhir")


class FixtureStore:
    """In-memory FHIR resources loaded from ``data/patients`` JSON files."""

    def __init__(self, files: list[Path], extra_docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        for f in files:
            doc = json.loads(f.read_text("utf-8"))
            self.docs[doc["id"]] = doc
        for doc in extra_docs or []:
            self.docs[doc["id"]] = doc

    def resources(self, pid: str, rtype: str) -> list[Resource]:
        doc = self.docs.get(pid)
        if doc is None:
            raise SourceUnavailableError("unknown patient")
        return [r for r in doc["resources"] if r["resourceType"] == rtype]


class FhirAdapter:
    name = "fhir"

    def __init__(
        self,
        store: FixtureStore,
        *,
        mode: str = "auto",
        base_url: str = "http://localhost:8090/fhir",
        timeout_s: float = 3.0,
        client: httpx.Client | None = None,
        demo_ids: list[str] | None = None,
    ) -> None:
        self._store = store
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_s)
        self._demo_ids = demo_ids
        self.requested_mode = mode
        self.mode = self._resolve_mode(mode)
        self.fell_back = mode == "auto" and self.mode == "fixtures"

    def _resolve_mode(self, mode: str) -> str:
        if mode in {"http", "fixtures"}:
            return mode
        try:
            r = self._client.get(f"{self._base}/metadata", params={"_summary": "true"})
            r.raise_for_status()
            logger.info("HAPI FHIR reachable at %s; using it", self._base)
            return "http"
        except httpx.HTTPError as exc:
            logger.warning(
                "MEDSAFE_FHIR_MODE=auto: HAPI FHIR not reachable (%s). SERVING BUNDLED FIXTURES instead; a FHIR outage "
                "will NOT fail open in this mode. Set MEDSAFE_FHIR_MODE=http or fixtures to be explicit.",
                exc,
            )
            return "fixtures"

    # ------------------------------------------------------------ http helpers
    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            r = self._client.get(f"{self._base}/{path}", params=params)
            r.raise_for_status()
            return dict(r.json())
        except httpx.HTTPError as exc:
            raise SourceUnavailableError(f"FHIR request failed: {exc}") from exc

    def _search(self, rtype: str, params: dict[str, str]) -> list[Resource]:
        out: list[Resource] = []
        bundle = self._get(rtype, {**params, "_count": "200"})
        while True:
            out += [e["resource"] for e in bundle.get("entry", [])]
            nxt = next((link["url"] for link in bundle.get("link", []) if link["relation"] == "next"), None)
            if not nxt:
                return out
            try:
                r = self._client.get(nxt)
                r.raise_for_status()
                bundle = r.json()
            except httpx.HTTPError as exc:
                raise SourceUnavailableError(f"FHIR paging failed: {exc}") from exc

    # ------------------------------------------------------------ interface
    def list_patients(self) -> list[PatientSummary]:
        ids = [pid for pid in (self._demo_ids or sorted(self._store.docs)) if pid in self._store.docs]
        live = self._patients_http(ids) if self.mode == "http" else {}
        out: list[PatientSummary] = []
        for pid in ids:
            doc = self._store.docs[pid]
            kind = "hand-authored" if pid.startswith("hand-") else "synthea-style"
            patient = live.get(pid) or next((r for r in doc["resources"] if r["resourceType"] == "Patient"), None)
            out.append(PatientSummary(pid, doc.get("label", pid), kind, doc.get("note", ""), patient))
        return out

    def _patients_http(self, ids: list[str]) -> dict[str, Resource]:
        """Demographics for the picker in one search (``Patient?_id=a,b,...``). On failure the bundled copy of the
        same Patient resource is used (``load_data.py`` loads these fixtures into HAPI), and that is logged."""
        if not ids:
            return {}
        try:
            found = self._search("Patient", {"_id": ",".join(ids)})
        except SourceUnavailableError as exc:
            logger.warning("HAPI Patient search failed; patient list uses the bundled demographics (%s)", exc)
            return {}
        return {str(r.get("id")): r for r in found if r.get("resourceType") == "Patient"}

    def get_patient(self, patient_id: str) -> Resource:
        if self.mode == "http":
            return self._get(f"Patient/{patient_id}")
        return self._store.resources(patient_id, "Patient")[0]

    def get_active_meds(self, patient_id: str) -> list[Resource]:
        if self.mode == "http":
            return self._search("MedicationRequest", {"patient": patient_id, "status": "active"})
        return [r for r in self._store.resources(patient_id, "MedicationRequest") if r.get("status") == "active"]

    def get_problems(self, patient_id: str) -> list[Resource]:
        if self.mode == "http":
            return self._search("Condition", {"patient": patient_id})
        return self._store.resources(patient_id, "Condition")

    def get_lab_series(self, patient_id: str) -> list[Resource]:
        if self.mode == "http":
            return self._search("Observation", {"patient": patient_id})
        return self._store.resources(patient_id, "Observation")

    def close(self) -> None:
        self._client.close()

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mode": self.mode,
            "base_url": self._base,
            "patients": len(self._store.docs),
            "requested_mode": self.requested_mode,
            "fell_back_to_fixtures": self.fell_back,
        }
        if self.mode == "http":
            try:
                self._get("metadata", {"_summary": "true"})
                info["reachable"] = True
            except SourceUnavailableError as exc:
                info.update(reachable=False, error=str(exc))
        else:
            info["reachable"] = True
        return info
