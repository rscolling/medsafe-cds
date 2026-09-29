from __future__ import annotations

import pytest

from app.adapters.base import SourceUnavailableError
from app.adapters.vista.adapter import OverlayRpcClient, VistaAdapter, load_overlay
from app.adapters.vista.rpc import VistaUnavailableError
from app.config import REPO_ROOT


def test_recorded_adapter_metformin_patient_meds(vista) -> None:  # type: ignore[no-untyped-def]
    meds = vista.get_active_meds("100157")
    assert [m["medicationCodeableConcept"]["text"] for m in meds] == ["METFORMIN HCL 500MG TAB"]
    labs = [o for o in vista.get_lab_series("100157") if o["code"]["coding"][0]["code"] != "29463-7"]
    assert labs == []  # documented: neither VEHU metformin patient has lab results (only a weight vital)


def test_recorded_adapter_lab_patient(vista) -> None:  # type: ignore[no-untyped-def]
    obs = vista.get_lab_series("100881")
    creat = sorted(o["effectiveDateTime"] for o in obs if o["code"]["coding"][0]["code"] == "2160-0")
    assert len(creat) >= 3 and creat[-1].startswith("2015-08-10")
    assert any(o["code"]["coding"][0]["code"] == "29463-7" for o in obs)  # weight from vitals


def test_all_three_interface_methods_are_fhir_shaped(vista) -> None:  # type: ignore[no-untyped-def]
    assert vista.get_patient("100881")["resourceType"] == "Patient"
    assert {m["resourceType"] for m in vista.get_active_meds("100157")} == {"MedicationRequest"}
    assert {c["resourceType"] for c in vista.get_problems("100881")} == {"Condition"}
    assert {o["resourceType"] for o in vista.get_lab_series("100881")} == {"Observation"}


def test_junk_patient_is_excluded(vista) -> None:  # type: ignore[no-untyped-def]
    assert "100897" not in {p.id for p in vista.list_patients()}
    with pytest.raises(SourceUnavailableError, match="excluded"):
        vista.get_patient("100897")


def test_overlay_patients_are_labeled_synthetic(vista) -> None:  # type: ignore[no-untyped-def]
    kinds = {p.id: p.synthetic_kind for p in vista.list_patients()}
    assert kinds["9000001"] == "vehu-synthetic-overlay"
    assert kinds["100881"] == "vehu"
    assert vista.get_patient("9000001")["meta"]["tag"][0]["code"] == "synthetic-overlay"


def test_unknown_patient_in_recorded_mode_raises_source_error(vista) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SourceUnavailableError):
        vista.get_patient("424242")


def test_cache_avoids_repeat_rpcs() -> None:
    calls: list[str] = []

    class Counting:
        def call(self, rpc: str, *p: str) -> str:
            calls.append(rpc)
            return {"ORWPT SELECT": "A,B^M^2500101^0", "ORWLRR NEWOLD": "^"}.get(rpc, "")

        def close(self) -> None: ...

    a = VistaAdapter(Counting(), mode="live", demo_patients=[{"dfn": "1", "label": "x"}], cache_ttl_s=60)
    a.get_patient("1")
    a.get_active_meds("1")
    a.get_problems("1")
    a.get_lab_series("1")
    assert calls.count("ORWPT SELECT") == 1


def test_live_failure_becomes_source_error() -> None:
    class Down:
        def call(self, rpc: str, *p: str) -> str:
            raise VistaUnavailableError("timeout")

        def close(self) -> None: ...

    a = VistaAdapter(Down(), mode="live", demo_patients=[{"dfn": "1", "label": "x"}])
    with pytest.raises(SourceUnavailableError, match="timeout"):
        a.get_patient("1")
    st = a.status()
    assert st["reachable"] is False and "timeout" in st["error"]


def test_status_recorded_and_overlay_client(vista) -> None:  # type: ignore[no-untyped-def]
    assert vista.status()["reachable"] is True
    ov = OverlayRpcClient(load_overlay(REPO_ROOT / "data/vista/overlay_patients.json"))
    assert ov.call("ORWLRR INTERIMG", "9000001", "3300101", "1", "1").startswith("8^CH^")
    assert ov.call("ORWLRR INTERIMG", "9000001", "1000101", "1", "1") == ""
    with pytest.raises(VistaUnavailableError):
        ov.call("ORWLRR INTERIMG", "5", "3300101", "1", "1")
    with pytest.raises(VistaUnavailableError, match="does not implement"):
        ov.call("NOPE", "9000001")
    assert load_overlay(REPO_ROOT / "nope.json") == {}
    ov.close()
