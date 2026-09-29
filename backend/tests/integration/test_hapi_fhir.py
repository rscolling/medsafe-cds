"""Against a real local HAPI FHIR server (docker compose up hapi; python scripts/load_data.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.adapters.fhir.adapter import FhirAdapter, FixtureStore
from app.config import REPO_ROOT
from app.rules.engine import RulesEngine
from app.service import CdsService, draft_order
from tests.integration.conftest import HAPI

pytestmark = pytest.mark.hapi
FILES = sorted((REPO_ROOT / "data/patients/hand-authored").glob("*.json"))


@pytest.fixture(scope="module")
def loaded() -> None:
    script = Path(REPO_ROOT / "scripts/load_data.py")
    subprocess.run(  # noqa: S603
        [sys.executable, str(script), "--fhir-base", HAPI, "--cohort-size", "5"], check=True, capture_output=True
    )


@pytest.fixture(scope="module")
def adapter(loaded: None) -> FhirAdapter:
    return FhirAdapter(FixtureStore(FILES), mode="http", base_url=HAPI, timeout_s=20)


def test_hapi_is_r4() -> None:
    cap = httpx.get(f"{HAPI}/metadata", timeout=10).json()
    assert cap["fhirVersion"].startswith("4.")


def test_adapter_reads_loaded_patient(adapter: FhirAdapter) -> None:
    assert adapter.mode == "http"
    assert adapter.get_patient("hand-01")["birthDate"]
    assert {m["status"] for m in adapter.get_active_meds("hand-04")} == {"active"}
    assert len(adapter.get_problems("hand-04")) == 3
    assert adapter.get_lab_series("hand-04")


@pytest.mark.parametrize("doc", [json.loads(p.read_text()) for p in FILES], ids=[p.stem for p in FILES])
def test_hapi_results_equal_fixture_results(adapter: FhirAdapter, doc: dict) -> None:  # type: ignore[type-arg]
    """The same scenarios give the same alerts whether data comes from HAPI or from the in-repo JSON."""
    settings_rules = REPO_ROOT / "rules"
    engine = RulesEngine.from_dir(settings_rules)
    live = CdsService(engine, {"fhir": adapter})
    fixtures = CdsService(engine, {"fhir": FhirAdapter(FixtureStore(FILES), mode="fixtures")})
    for sc in doc["scenarios"]:
        for mode in ("baseline", "context"):
            a = {x.rule_id for x in live.evaluate("fhir", doc["id"], sc["order"], mode).result.alerts}
            b = {x.rule_id for x in fixtures.evaluate("fhir", doc["id"], sc["order"], mode).result.alerts}
            assert a == b == set(sc[f"expect_{mode}"])


def test_draft_order_helper_against_hapi(adapter: FhirAdapter) -> None:
    engine = RulesEngine.from_dir(REPO_ROOT / "rules")
    svc = CdsService(engine, {"fhir": adapter})
    res = svc.evaluate("fhir", "hand-01", draft_order("hand-01", "861007"), "context")
    assert [a.rule_id for a in res.result.alerts] == ["metformin-low-egfr"]
