"""Against a live worldvista/vehu container (RPC Broker :9430). Opt-in: needs VISTA_ACCESS_CODE/VERIFY_CODE.

docker run -d -p 127.0.0.1:9430:9430 worldvista/vehu   # ~6.7 GB image, 60-75 s to boot
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


def _comparable(rpc: str, reply: str) -> object:
    # ORWPT SELECT carries the SSN placeholder (recorded as 000000000) and volatile session pieces: compare the
    # stable identity pieces only (name, sex, DOB).
    return reply.split("^")[:3] if rpc == "ORWPT SELECT" else reply


def test_live_equals_recorded_for_all_recorded_calls(live: VistaAdapter) -> None:
    """Drift check: re-issue EVERY recorded call (3,275 read-only RPCs) against the live server and compare replies.

    MEDSAFE_DRIFT_SAMPLE=<n> limits it to the first n calls for a quick run (default: all).
    """
    rec = RecordedRpcClient(REPO_ROOT / "data/vista/recorded/index.json")
    calls = rec.known_calls()
    limit = int(os.environ.get("MEDSAFE_DRIFT_SAMPLE", "0")) or len(calls)
    mismatches = []
    for rpc, params in calls[:limit]:
        got = live._client.call(rpc, *params)  # noqa: SLF001
        if _comparable(rpc, got) != _comparable(rpc, rec.call(rpc, *params)):
            mismatches.append((rpc, params))
    assert not mismatches, f"{len(mismatches)}/{limit} drifted; first: {mismatches[:3]}"


def test_live_urine_creatinine_never_becomes_egfr(live: VistaAdapter) -> None:
    """H3 on the real server: DFN 100881 as of 2015-07-06 used to report eGFR 0.1 from a urine creatinine."""
    from datetime import date

    from app.context import build_context

    ctx = build_context(
        live.get_patient("100881"),
        live.get_active_meds("100881"),
        live.get_problems("100881"),
        live.get_lab_series("100881"),
        source="vista",
        as_of=date(2015, 7, 6),
    )
    assert all(v.value >= 1 for v in ctx.labs["egfr"]), [v.value for v in ctx.labs["egfr"]]
    assert ctx.latest_lab("creatinine").value < 20  # type: ignore[union-attr]


def test_live_error_replies_are_errors_not_data(live: VistaAdapter) -> None:
    """H1 on the real server: an M trap and a missing RPC must raise, never parse as an empty record."""
    from app.adapters.vista.rpc import VistaUnavailableError

    with pytest.raises(VistaUnavailableError):
        live._client.call("ORWPT SELECT", "1;2")  # noqa: SLF001  # M  ERROR=...
    with pytest.raises(VistaUnavailableError, match="doesn't exist"):
        live._client.call("NO SUCH RPC X")  # noqa: SLF001
    assert live._client.call("ORWPT SELECT", "100881")  # noqa: SLF001  # the connection survives both


def test_live_leading_zero_junk_dfn_is_excluded(live: VistaAdapter) -> None:
    from app.adapters.base import SourceUnavailableError

    live._excluded = {"100897"}  # noqa: SLF001
    with pytest.raises(SourceUnavailableError, match="excluded"):
        live.get_patient("0100897")


def test_live_same_rule_fires_for_vehu_and_twin(live: VistaAdapter) -> None:
    engine = RulesEngine.from_dir(REPO_ROOT / "rules")
    svc = CdsService(engine, {"vista": live})
    for dfn in ("100881", "9000001"):
        res = svc.evaluate("vista", dfn, draft_order(dfn, "861007"), "context")
        assert [a.rule_id for a in res.result.alerts] == ["metformin-low-egfr"], dfn
    ctx = svc.load_context("vista", "100881")
    assert ctx.latest_lab("egfr").source == "computed"  # type: ignore[union-attr]  # VEHU has no eGFR


@pytest.mark.parametrize("dfn", ["99999999", "0"])
def test_live_nonexistent_patient_is_source_unavailable_not_empty_record(live: VistaAdapter, dfn: str) -> None:
    """VEHU answers ORWPT SELECT with '-1^...unknown to CPRS' for a missing DFN; that must not look like 'no data'."""
    from app.adapters.base import SourceUnavailableError

    with pytest.raises(SourceUnavailableError):
        live.get_patient(dfn)


def test_live_garbage_patient_id_never_reaches_the_broker(live: VistaAdapter) -> None:
    from app.adapters.base import SourceUnavailableError

    with pytest.raises(SourceUnavailableError, match="invalid VistA patient id"):
        live.get_patient("abc")
