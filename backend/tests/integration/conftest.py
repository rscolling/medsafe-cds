"""Integration tests need containers; they SKIP (never fail) when a container is unavailable."""

from __future__ import annotations

import os
import socket

import httpx
import pytest

HAPI = os.environ.get("MEDSAFE_FHIR_BASE_URL", "http://localhost:8090/fhir")
VISTA_HOST = os.environ.get("VISTA_HOST", "localhost")
VISTA_PORT = int(os.environ.get("VISTA_PORT", "9430"))


def hapi_up() -> bool:
    try:
        return httpx.get(f"{HAPI}/metadata", params={"_summary": "true"}, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def vista_up() -> bool:
    if not (os.environ.get("VISTA_ACCESS_CODE") and os.environ.get("VISTA_VERIFY_CODE")):
        return False
    try:
        with socket.create_connection((VISTA_HOST, VISTA_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" not in str(item.fspath):
            continue
        item.add_marker(pytest.mark.integration)
        if "hapi" in item.keywords and not hapi_up():
            item.add_marker(pytest.mark.skip(reason=f"HAPI FHIR not reachable at {HAPI} (docker compose up hapi)"))
        if "vista_live" in item.keywords and not vista_up():
            item.add_marker(
                pytest.mark.skip(reason="VEHU RPC Broker unavailable or VISTA_ACCESS_CODE/VISTA_VERIFY_CODE unset")
            )
