"""RPC layer: recorded replay, live-broker wrapper (faked), and the vendored parse_response fix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from vista_clients.rpc.errors import RPCError
from vista_clients.rpc.protocol import parse_response

from app.adapters.vista.rpc import (
    BrokerRpcClient,
    RecordedRpcClient,
    RecordingRpcClient,
    VistaUnavailableError,
    call_key,
)


def test_parse_response_no_data_found_is_data_not_error() -> None:
    """Upstream bug: replies starting with a control byte (CR/LF) were misread as SNDERR."""
    resp = parse_response("\r\nNo Data Found\r\n", had_null_prefix=True)
    assert "No Data Found" in resp.raw


def test_parse_response_real_error_packet_still_raises() -> None:
    err = "\x00" + chr(9) + "Bad thing"  # empty security packet + application error packet
    with pytest.raises(RPCError, match="Bad thing"):
        parse_response(err)


def test_parse_response_legacy_heuristic_when_flag_unknown() -> None:
    with pytest.raises(RPCError):
        parse_response("\x05Oops!")


def test_recorded_client_roundtrip(tmp_path: Path) -> None:
    key = call_key("ORWPT SELECT", ("1",))
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({key: {"rpc": "ORWPT SELECT", "params": json.dumps(["1"]), "reply": "X^M^2000101"}}))
    c = RecordedRpcClient(idx)
    assert c.call("ORWPT SELECT", "1") == "X^M^2000101"
    assert c.known_calls() == [("ORWPT SELECT", ("1",))]
    with pytest.raises(VistaUnavailableError, match="no recorded reply"):
        c.call("ORWPT SELECT", "2")
    c.close()


def test_recording_client_captures() -> None:
    class Fake:
        def call(self, rpc: str, *params: str) -> str:
            return f"{rpc}:{params}"

        def close(self) -> None:
            self.closed = True

    rec = RecordingRpcClient(Fake())
    assert rec.call("A", "1") == "A:('1',)"
    assert call_key("A", ("1",)) in rec.recorded
    rec.close()


class FakeBroker:
    instances: list[FakeBroker] = []

    def __init__(self, host: str, port: int, timeout: float, app_name: str) -> None:
        self.calls: list[str] = []
        self.fail_next = False
        FakeBroker.instances.append(self)

    def connect(self) -> None: ...
    def authenticate(self, a: str, v: str) -> str:
        return "1"

    def create_context(self, c: str) -> None: ...
    def disconnect(self) -> None: ...

    def call_rpc(self, name: str, params: Any) -> Any:
        from vista_clients.rpc.errors import BrokerConnectionError

        if name == "BOOM":
            raise RPCError("nope")
        if getattr(FakeBroker, "die_once", False):
            FakeBroker.die_once = False
            raise BrokerConnectionError("socket died")
        self.calls.append(name)

        class R:
            raw = f"reply:{name}:{len(params)}"

        return R()


@pytest.fixture
def patched_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    import vista_clients.rpc as rpc

    FakeBroker.instances.clear()
    FakeBroker.die_once = False  # type: ignore[attr-defined]
    monkeypatch.setattr(
        rpc,
        "VistABroker",
        lambda host, port, timeout, app_name: FakeBroker(host, port, timeout, app_name),
    )


def test_broker_client_reuses_connection(patched_broker: None) -> None:
    c = BrokerRpcClient("h", 1, "a", "v", "ctx", 1.0)
    assert c.call("X", "1", "2") == "reply:X:2"
    assert c.call("Y") == "reply:Y:0"
    assert len(FakeBroker.instances) == 1
    c.close()


def test_broker_client_reconnects_once_then_succeeds(patched_broker: None) -> None:
    c = BrokerRpcClient("h", 1, "a", "v", "ctx", 1.0)
    c.call("X")
    FakeBroker.die_once = True  # type: ignore[attr-defined]
    assert c.call("Y") == "reply:Y:0"
    assert len(FakeBroker.instances) == 2


def test_broker_client_application_error_keeps_connection(patched_broker: None) -> None:
    c = BrokerRpcClient("h", 1, "a", "v", "ctx", 1.0)
    with pytest.raises(VistaUnavailableError, match="BOOM"):
        c.call("BOOM")
    assert len(FakeBroker.instances) == 1


def test_broker_client_gives_up_after_second_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import vista_clients.rpc as rpc
    from vista_clients.rpc.errors import BrokerConnectionError

    def refuse(*a: Any, **k: Any) -> Any:
        raise BrokerConnectionError("refused")

    monkeypatch.setattr(rpc, "VistABroker", refuse)
    with pytest.raises(VistaUnavailableError, match="refused"):
        BrokerRpcClient("h", 1, "a", "v", "ctx", 1.0).call("X")
