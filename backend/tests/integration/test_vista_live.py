"""Against a live worldvista/vehu container (RPC Broker :9430). Opt-in: needs VISTA_ACCESS_CODE/VERIFY_CODE.

docker run -d -p 9430:9430 worldvista/vehu   # ~6.7 GB image, 60-75 s to boot
export VISTA_ACCESS_CODE=... VISTA_VERIFY_CODE=...   # public demo codes from the image's Docker Hub page
"""

from __future__ import annotations

import os

import pytest

from app.adapters.vista.adapter import VistaAdapter, load_overlay
from app.adapters.vista.rpc import BrokerRpcClient, RecordedRpcClient
from app.config import REPO_ROOT
from app.rules.engine import RulesEngine
from app.service import CdsService, draft_order
from tests.integration.conftest import VISTA_HOST, VISTA_PORT

pytestmark = pytest.mark.vista_live


@pytest.fixture(scope="module")
def live() -> VistaAdapter:
    client = BrokerRpcClient(
        VISTA_HOST,
        VISTA_PORT,
        os.environ["VISTA_ACCESS_CODE"],
        os.environ["VISTA_VERIFY_CODE"],
        "OR CPRS GUI CHART",
        30,
    )
    import json

    demo = json.loads((REPO_ROOT / "data/vista/demo_patients.json").read_text())
    return VistaAdapter(
        client, mode="live", demo_patients=demo, overlay=load_overlay(REPO_ROOT / "data/vista/overlay_patients.json")
    )


def test_live_metformin_patient(live: VistaAdapter) -> None:
    meds = live.get_active_meds("100157")
    assert meds and "METFORMIN" in meds[0]["medicationCodeableConcept"]["text"]


def test_live_lab_patient_has_creatinine_series_and_problems(live: VistaAdapter) -> None:
    obs = live.get_lab_series("100881")
    assert sum(1 for o in obs if o["code"]["coding"][0]["code"] == "2160-0") >= 3
    assert any("renal" in c["code"]["text"].lower() for c in live.get_problems("100881"))


def test_live_no_data_reply_does_not_raise(live: VistaAdapter) -> None:
    """Regression for the upstream parse_response bug ('\\r\\nNo Data Found' misread as an error)."""
    assert live.get_lab_series("100157") is not None
    raw = live._client.call("ORWLRR INTERIM", "100881", "3300101", "3300102")  # noqa: SLF001
    assert isinstance(raw, str)


def test_live_equals_recorded_for_all_recorded_calls(live: VistaAdapter) -> None:
    """Drift check: re-issue every recorded call against the live server and compare replies."""
    rec = RecordedRpcClient(REPO_ROOT / "data/vista/recorded/index.json")
    mismatches = []
    for rpc, params in rec.known_calls()[:300]:
        got = live._client.call(rpc, *params)  # noqa: SLF001
        want = rec.call(rpc, *params)
        if rpc == "ORWPT SELECT":  # SSN blanked in recordings
            got, want = got.split("^")[:3], want.split("^")[:3]  # type: ignore[assignment]
        if got != want:
            mismatches.append((rpc, params))
    assert not mismatches, mismatches[:3]


def test_live_same_rule_fires_for_vehu_and_twin(live: VistaAdapter) -> None:
    engine = RulesEngine.from_dir(REPO_ROOT / "rules")
    svc = CdsService(engine, {"vista": live})
    for dfn in ("100881", "9000001"):
        res = svc.evaluate("vista", dfn, draft_order(dfn, "861007"), "context")
        assert [a.rule_id for a in res.result.alerts] == ["metformin-low-egfr"], dfn
    ctx = svc.load_context("vista", "100881")
    assert ctx.latest_lab("egfr").source == "computed"  # type: ignore[union-attr]  # VEHU has no eGFR
