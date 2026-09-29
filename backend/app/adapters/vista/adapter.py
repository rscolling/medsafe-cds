"""VistA data source: RPC Broker (live) or recorded replies, mapped to FHIR-shaped resources.

RPCs used (all proven against ``worldvista/vehu``):

* ``ORWPT SELECT``      demographics (name, sex, DOB)
* ``ORWPS ACTIVE``      current medications (free-text names)
* ``ORQQPL LIST``       active problem list
* ``ORWLRR NEWOLD``     cheap "does this patient have any labs" check
* ``ORWLRR INTERIMG``   lab collections, one per call, paged backwards by date
* ``ORQQVI VITALS``     latest vitals (weight for Cockcroft-Gault)

A per-patient cache (TTL) avoids re-issuing ~10 RPCs per hook call.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.adapters.base import PatientSummary, Resource, SourceUnavailableError
from app.adapters.vista import parsing
from app.adapters.vista.rpc import RecordedRpcClient, RpcClient, VistaUnavailableError

logger = logging.getLogger("medsafe.vista")

MAX_LAB_SETS = 30  # safety cap on backwards paging (VEHU max observed for real patients ~60)
FAR_FUTURE_FM = "3300101"
_DFN_RE = re.compile(r"[0-9]{1,12}")


@dataclass
class _Bundle:
    patient: Resource
    meds: list[Resource]
    problems: list[Resource]
    observations: list[Resource]
    fetched_at: float


@dataclass(frozen=True)
class OverlayPatient:
    """Synthetic patient that lives *in VistA wire format* (raw RPC replies), not in VEHU itself."""

    dfn: str
    label: str
    note: str
    replies: dict[str, str]


def load_overlay(path: Path) -> dict[str, OverlayPatient]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text("utf-8"))
    return {p["dfn"]: OverlayPatient(p["dfn"], p["label"], p["note"], p["replies"]) for p in data["patients"]}


class OverlayRpcClient:
    """Serves overlay patients' replies in exactly the format the live broker returns."""

    def __init__(self, patients: dict[str, OverlayPatient]) -> None:
        self._patients = patients

    def call(self, rpc: str, *params: str) -> str:
        p = self._patients.get(params[0]) if params else None
        if p is None:
            raise VistaUnavailableError(f"unknown overlay patient for {rpc}")
        if rpc == "ORWLRR INTERIMG":
            before = params[1]
            sets = sorted(p.replies.get("labs", []), key=lambda s: s.split("^")[2], reverse=True)
            for s in sets:
                if s.split("^")[2] < before:
                    return str(s)
            return ""
        key = {
            "ORWPT SELECT": "select",
            "ORWPS ACTIVE": "meds",
            "ORQQPL LIST": "problems",
            "ORQQVI VITALS": "vitals",
            "ORWLRR NEWOLD": "newold",
        }.get(rpc)
        if key is None:
            raise VistaUnavailableError(f"overlay does not implement {rpc}")
        return str(p.replies.get(key, ""))

    def close(self) -> None:
        return None


class VistaAdapter:
    name = "vista"

    def __init__(
        self,
        client: RpcClient,
        *,
        mode: str,
        demo_patients: list[dict[str, str]],
        overlay: dict[str, OverlayPatient] | None = None,
        excluded_dfns: tuple[str, ...] = ("100897",),
        cache_ttl_s: float = 60.0,
    ) -> None:
        self._client = client
        self.mode = mode  # "live" | "recorded"
        self._demo = demo_patients
        self._overlay = overlay or {}
        self._overlay_client = OverlayRpcClient(self._overlay)
        self._excluded = set(excluded_dfns)
        self._ttl = cache_ttl_s
        self._cache: dict[str, _Bundle] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers
    def _rpc(self, patient_id: str) -> RpcClient:
        return self._overlay_client if patient_id in self._overlay else self._client

    def _bundle(self, patient_id: str) -> _Bundle:
        if not _DFN_RE.fullmatch(patient_id):
            raise SourceUnavailableError("invalid VistA patient id (DFN must be digits)")
        if patient_id in self._excluded:
            raise SourceUnavailableError("patient is excluded from demos (junk data)")
        with self._lock:
            hit = self._cache.get(patient_id)
            if hit and time.monotonic() - hit.fetched_at < self._ttl:
                return hit
        try:
            bundle = self._fetch(patient_id)
        except VistaUnavailableError as exc:
            raise SourceUnavailableError(str(exc)) from exc
        except ValueError as exc:  # nonexistent patient / unparseable reply: never guess, never "no data"
            raise SourceUnavailableError(str(exc)) from exc
        with self._lock:
            self._cache[patient_id] = bundle
        return bundle

    def _fetch(self, dfn: str) -> _Bundle:
        rpc = self._rpc(dfn)
        patient = parsing.parse_patient_select(dfn, rpc.call("ORWPT SELECT", dfn))
        if dfn in self._overlay:
            patient["meta"] = {"tag": [{"system": "urn:medsafe:data", "code": "synthetic-overlay"}]}
        meds = [
            r
            for m in parsing.parse_active_meds(rpc.call("ORWPS ACTIVE", dfn, "0", "1", "0"))
            if (r := parsing.med_to_medication_request(dfn, m)) is not None
        ]
        problems = parsing.parse_problems(dfn, rpc.call("ORQQPL LIST", dfn, "A"))
        observations = self._fetch_labs(rpc, dfn)
        try:
            observations += parsing.parse_weight(dfn, rpc.call("ORQQVI VITALS", dfn, "", ""))
        except VistaUnavailableError:
            logger.debug("no recorded vitals")
        return _Bundle(patient, meds, problems, observations, time.monotonic())

    def _fetch_labs(self, rpc: RpcClient, dfn: str) -> list[Resource]:
        newest, _ = parsing.newest_oldest(rpc.call("ORWLRR NEWOLD", dfn))
        if newest is None:
            return []
        sets: list[parsing.LabSet] = []
        before = FAR_FUTURE_FM
        for _ in range(MAX_LAB_SETS):
            s = parsing.parse_lab_set(rpc.call("ORWLRR INTERIMG", dfn, before, "1", "1"))
            if s is None or s.collected >= before:
                break
            sets.append(s)
            before = s.collected
        return parsing.lab_sets_to_observations(dfn, sets)

    # ------------------------------------------------------------ interface
    def list_patients(self) -> list[PatientSummary]:
        out = [
            PatientSummary(p["dfn"], p["label"], p.get("kind", "vehu"), p.get("note", ""))
            for p in self._demo
            if p["dfn"] not in self._excluded
        ]
        out += [PatientSummary(o.dfn, o.label, "vehu-synthetic-overlay", o.note) for o in self._overlay.values()]
        return out

    def get_patient(self, patient_id: str) -> Resource:
        return self._bundle(patient_id).patient

    def get_active_meds(self, patient_id: str) -> list[Resource]:
        return list(self._bundle(patient_id).meds)

    def get_problems(self, patient_id: str) -> list[Resource]:
        return list(self._bundle(patient_id).problems)

    def get_lab_series(self, patient_id: str) -> list[Resource]:
        return list(self._bundle(patient_id).observations)

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"mode": self.mode, "overlay_patients": len(self._overlay)}
        if self.mode == "live":
            try:
                self._client.call("ORWPT SELECT", self._demo[0]["dfn"])
                info["reachable"] = True
            except VistaUnavailableError as exc:
                info.update(reachable=False, error=str(exc))
        else:
            info["reachable"] = True
        return info


def recorded_client(recorded_dir: Path) -> RecordedRpcClient:
    return RecordedRpcClient(recorded_dir / "index.json")
