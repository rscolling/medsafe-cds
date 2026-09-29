"""Wire up settings -> adapters (built once at startup)."""

from __future__ import annotations

import logging

from app.adapters.base import PatientSource
from app.adapters.fhir.adapter import FhirAdapter, FixtureStore
from app.adapters.vista.adapter import VistaAdapter, load_overlay, recorded_client
from app.adapters.vista.rpc import BrokerRpcClient, RpcClient
from app.cohort import generate_cohort
from app.config import Settings

logger = logging.getLogger("medsafe.registry")


def build_fhir(settings: Settings, cohort_size: int = 300) -> FhirAdapter:
    files = sorted((settings.data_dir / "patients" / "hand-authored").glob("*.json"))
    store = FixtureStore(files, generate_cohort(cohort_size))
    demo_ids = [f.stem for f in files] + [f"syn-{i:04d}" for i in range(1, 9)]
    return FhirAdapter(
        store,
        mode=settings.fhir_mode,
        base_url=settings.fhir_base_url,
        timeout_s=settings.fhir_timeout_s,
        demo_ids=demo_ids,
    )


def _live_reachable(settings: Settings) -> bool:
    import socket

    try:
        with socket.create_connection((settings.vista_host, settings.vista_port), timeout=1.5):
            return True
    except OSError:
        return False


def build_vista(settings: Settings, client: RpcClient | None = None, mode: str | None = None) -> VistaAdapter:
    vdir = settings.data_dir / "vista"
    import json

    demo = json.loads((vdir / "demo_patients.json").read_text("utf-8"))
    overlay = load_overlay(vdir / "overlay_patients.json")
    chosen = mode or settings.vista_mode
    if client is None:
        have_creds = bool(settings.vista_access and settings.vista_verify)
        if chosen == "auto":
            chosen = "live" if (have_creds and _live_reachable(settings)) else "recorded"
            logger.info("VistA mode auto-selected: %s", chosen)
        if chosen == "live":
            if not have_creds:
                raise RuntimeError("VISTA_ACCESS_CODE / VISTA_VERIFY_CODE must be set for live VistA mode")
            client = BrokerRpcClient(
                settings.vista_host,
                settings.vista_port,
                settings.vista_access,
                settings.vista_verify,
                settings.vista_context,
                settings.vista_timeout_s,
            )
        else:
            client = recorded_client(vdir / "recorded")
    return VistaAdapter(
        client,
        mode=chosen,
        demo_patients=demo,
        overlay=overlay,
        excluded_dfns=settings.vista_excluded_dfns,
        cache_ttl_s=settings.vista_cache_ttl_s,
    )


def build_sources(settings: Settings) -> dict[str, PatientSource]:
    return {"fhir": build_fhir(settings), "vista": build_vista(settings)}


__all__ = ["build_fhir", "build_sources", "build_vista"]
