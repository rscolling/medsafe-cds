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
from app.adapters.vista.fileman import fileman_key
from app.adapters.vista.rpc import RecordedRpcClient, RpcClient, VistaUnavailableError

logger = logging.getLogger("medsafe.vista")

# Safety cap on backwards paging, one INTERIMG call per collection. Measured on VEHU: real patients have up to ~150
# collections (DFN 100000 has 147). 200 leaves headroom; hitting it is logged and flagged (never silent).
MAX_LAB_SETS = 200
FAR_FUTURE_FM = "3300101"
MAX_CACHE_ENTRIES = 512
STATUS_TTL_S = 10.0
TRUNCATED_TAG = {"system": "urn:medsafe:data", "code": "labs-truncated"}
_DFN_RE = re.compile(r"[0-9]{1,12}")


@dataclass
class _Bundle:
    patient: Resource
    meds: list[Resource]
    problems: list[Resource]
    observations: list[Resource]
    fetched_at: float
    labs_truncated: bool = False


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
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        fetch_deadline_s: float = 10.0,
        max_lab_sets: int = MAX_LAB_SETS,
        pending_active: bool = True,
    ) -> None:
        self._client = client
        self.mode = mode  # "live" | "recorded"
        self._demo = demo_patients
        self._overlay = overlay or {}
        self._overlay_client = OverlayRpcClient(self._overlay)
        self._excluded = {_normalise_dfn(d) or d for d in excluded_dfns}
        self._ttl = cache_ttl_s
        self._max_cache = max(1, max_cache_entries)
        self._deadline_s = fetch_deadline_s
        self._max_lab_sets = max_lab_sets
        self._pending_active = pending_active
        self._cache: dict[str, _Bundle] = {}
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Lock] = {}  # per-DFN single-flight
        self._status: tuple[float, dict[str, Any]] | None = None
        self.truncated_fetches = 0  # counter: lab paging cap or unparseable page hit
        self.head_disagreements = 0  # counter: NEWOLD newest != INTERIMG chain head (L15)

    # ------------------------------------------------------------ helpers
    def _rpc(self, patient_id: str) -> RpcClient:
        return self._overlay_client if patient_id in self._overlay else self._client

    def _bundle(self, patient_id: str) -> _Bundle:
        dfn = _normalise_dfn(patient_id)
        if dfn is None:
            raise SourceUnavailableError("invalid VistA patient id (DFN must be a positive integer)")
        if dfn in self._excluded:
            raise SourceUnavailableError("patient is excluded from demos (junk data)")
        hit = self._cached(dfn)
        if hit is not None:
            return hit
        with self._lock:
            gate = self._inflight.setdefault(dfn, threading.Lock())
        with gate:  # single-flight: concurrent requests for one DFN share one fetch
            try:
                hit = self._cached(dfn)
                if hit is not None:
                    return hit
                try:
                    bundle = self._fetch(dfn)
                except VistaUnavailableError as exc:
                    raise SourceUnavailableError(str(exc)) from exc
                except ValueError as exc:  # nonexistent patient / unparseable reply: never guess, never "no data"
                    raise SourceUnavailableError(str(exc)) from exc
                self._store(dfn, bundle)
                return bundle
            finally:
                with self._lock:
                    self._inflight.pop(dfn, None)

    def _cached(self, dfn: str) -> _Bundle | None:
        with self._lock:
            hit = self._cache.get(dfn)
            if hit and time.monotonic() - hit.fetched_at < self._ttl:
                return hit
        return None

    def _store(self, dfn: str, bundle: _Bundle) -> None:
        """Bounded cache: drop expired entries first, then the oldest, so memory cannot grow without limit."""
        with self._lock:
            self._cache[dfn] = bundle
            if len(self._cache) > self._max_cache:
                now = time.monotonic()
                for k in [k for k, v in self._cache.items() if now - v.fetched_at >= self._ttl]:
                    del self._cache[k]
                while len(self._cache) > self._max_cache:
                    del self._cache[min(self._cache, key=lambda k: self._cache[k].fetched_at)]

    def _fetch(self, dfn: str) -> _Bundle:
        rpc: RpcClient = _Deadline(self._rpc(dfn), self._deadline_s)
        patient = parsing.parse_patient_select(dfn, rpc.call("ORWPT SELECT", dfn))
        if dfn in self._overlay:
            patient["meta"] = {"tag": [{"system": "urn:medsafe:data", "code": "synthetic-overlay"}]}
        meds = [
            r
            for m in parsing.parse_active_meds(rpc.call("ORWPS ACTIVE", dfn, "0", "1", "0"))
            if (r := parsing.med_to_medication_request(dfn, m, pending_active=self._pending_active)) is not None
        ]
        problems = parsing.parse_problems(dfn, rpc.call("ORQQPL LIST", dfn, "A"))
        observations, truncated = self._fetch_labs(rpc, dfn)
        if truncated:
            patient.setdefault("meta", {}).setdefault("tag", []).append(dict(TRUNCATED_TAG))
        try:
            observations += parsing.parse_weight(dfn, rpc.call("ORQQVI VITALS", dfn, "", ""))
        except VistaUnavailableError:
            logger.debug("no recorded vitals")
        return _Bundle(patient, meds, problems, observations, time.monotonic(), labs_truncated=truncated)

    def _fetch_labs(self, rpc: RpcClient, dfn: str) -> tuple[list[Resource], bool]:
        """Page lab collections newest to oldest. Returns ``(observations, truncated)``.

        ``truncated`` is True when paging stopped for any reason other than reaching the oldest collection
        (cap hit, unparseable page). It is logged, counted and tagged on the Patient, never silent.
        """
        raw_newold = rpc.call("ORWLRR NEWOLD", dfn)
        if parsing.newold_is_garbage(raw_newold):
            # A reply that is neither "^" (no labs) nor a FileMan pair is an error in disguise, not "no labs".
            raise ValueError("ORWLRR NEWOLD returned an unparseable reply")
        head = parsing.newest_raw(raw_newold)
        if not head:
            return [], False
        sets: list[parsing.LabSet] = []
        before = FAR_FUTURE_FM
        truncated = False
        for _ in range(self._max_lab_sets):
            raw = rpc.call("ORWLRR INTERIMG", dfn, before, "1", "1")
            if not raw.strip():
                break  # no collection older than `before`: reached the oldest
            s = parsing.parse_lab_set(raw)
            key, prev = (fileman_key(s.collected) if s else None), fileman_key(before)
            if s is None or key is None or (prev is not None and key >= prev):
                logger.warning("lab paging stopped on an unusable page", extra={"rpc": "ORWLRR INTERIMG"})
                truncated = True
                break
            sets.append(s)
            before = s.collected
        else:
            truncated = True
        if truncated:
            self.truncated_fetches += 1
            logger.warning(
                "lab history may be incomplete (paging cap %d or bad page): treat 'no recent lab' as unknown",
                self._max_lab_sets,
            )
        if sets and fileman_key(head) != fileman_key(sets[0].collected):
            self.head_disagreements += 1
            logger.warning("ORWLRR NEWOLD newest does not match the INTERIMG chain head; using the chain")
        elif not sets:
            self.head_disagreements += 1
            logger.warning("ORWLRR NEWOLD reports labs but INTERIMG returned none")
        return parsing.lab_sets_to_observations(dfn, sets), truncated

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
        """Cheap, bounded status: no RPC under the broker lock and at most one probe per STATUS_TTL_S."""
        info: dict[str, Any] = {
            "mode": self.mode,
            "overlay_patients": len(self._overlay),
            "cached_patients": len(self._cache),
            "truncated_lab_fetches": self.truncated_fetches,
            "lab_head_disagreements": self.head_disagreements,
            "unknown_med_statuses": dict(parsing.UNKNOWN_STATUS_COUNTS),
        }
        if self.mode != "live":
            info["reachable"] = True
            return info
        health = getattr(self._client, "health", None)
        snap: dict[str, Any] = health() if callable(health) else {}
        if snap:
            info["broker"] = snap
        if snap.get("state") in ("auth-blocked", "circuit-open"):
            info.update(reachable=False, error=snap.get("last_error", "VistA unavailable"))
            return info
        now = time.monotonic()
        if self._status is not None and now - self._status[0] < STATUS_TTL_S:
            return {**info, **self._status[1]}
        try:
            self._client.call("ORWPT SELECT", self._demo[0]["dfn"])
            probe: dict[str, Any] = {"reachable": True}
        except VistaUnavailableError as exc:
            probe = {"reachable": False, "error": str(exc)}
        self._status = (now, probe)
        return {**info, **probe}

    def close(self) -> None:
        self._client.close()


def _normalise_dfn(raw: str) -> str | None:
    """Canonical DFN: digits only, no leading zeros, > 0. '0100897' and '100897' are the same patient."""
    if not _DFN_RE.fullmatch(raw):
        return None
    n = int(raw)
    return str(n) if n > 0 else None


class _Deadline:
    """Wraps an RpcClient so one patient fetch has a total time budget (a slow broker cannot hold a thread)."""

    def __init__(self, inner: RpcClient, budget_s: float) -> None:
        self._inner, self._end = inner, time.monotonic() + budget_s

    def call(self, rpc: str, *params: str) -> str:
        if time.monotonic() > self._end:
            raise VistaUnavailableError("VistA fetch exceeded its time budget")
        return self._inner.call(rpc, *params)

    def close(self) -> None:
        self._inner.close()


def recorded_client(recorded_dir: Path) -> RecordedRpcClient:
    return RecordedRpcClient(recorded_dir / "index.json")
