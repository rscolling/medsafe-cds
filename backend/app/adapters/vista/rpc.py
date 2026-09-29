"""Low-level RPC access to VistA.

Two interchangeable implementations of :class:`RpcClient`:

* :class:`BrokerRpcClient` - live XWB RPC Broker (vendored CivicActions/vista-clients, Apache-2.0).
  The broker connection is stateful and **not thread-safe**, so this class owns exactly one
  connection guarded by a lock, reconnects on failure and uses socket timeouts.
* :class:`RecordedRpcClient` - replays raw broker replies captured from a real VEHU container
  (``data/vista/recorded``), so CI and offline demos never need the 6.7 GB image.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("medsafe.vista.rpc")


class VistaUnavailableError(RuntimeError):
    """The VistA source could not be reached or answered with a transport error."""


class RpcClient(Protocol):
    """Send one RPC with literal string params; return the raw reply (CRLF separated)."""

    def call(self, rpc: str, *params: str) -> str: ...

    def close(self) -> None: ...


def call_key(rpc: str, params: tuple[str, ...]) -> str:
    """Stable key for a recorded call (also used as fixture file name)."""
    digest = hashlib.sha1("\x1f".join((rpc, *params)).encode(), usedforsecurity=False).hexdigest()
    return digest[:16]


class BrokerRpcClient:
    """Live RPC Broker client (single locked connection, lazily (re)connected)."""

    def __init__(
        self,
        host: str,
        port: int,
        access: str,
        verify: str,
        context: str,
        timeout_s: float = 8.0,
    ) -> None:
        self._host, self._port = host, port
        self._access, self._verify, self._context = access, verify, context
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._broker: object | None = None

    def _connect(self) -> object:
        from vista_clients.rpc import VistABroker  # vendored, see backend/third_party

        broker = VistABroker(self._host, self._port, timeout=self._timeout, app_name="medsafe-cds")
        broker.connect()
        broker.authenticate(self._access, self._verify)
        broker.create_context(self._context)
        logger.info("vista broker connected", extra={"host": self._host, "port": self._port})
        return broker

    def call(self, rpc: str, *params: str) -> str:
        from vista_clients.rpc import literal
        from vista_clients.rpc.errors import RPCError, VistAError

        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._broker is None:
                        self._broker = self._connect()
                    resp = self._broker.call_rpc(rpc, [literal(str(p)) for p in params])  # type: ignore[attr-defined]
                    return str(resp.raw)
                except RPCError as exc:  # application-level error: connection still good
                    raise VistaUnavailableError(f"RPC {rpc} failed: {exc}") from exc
                except (VistAError, OSError) as exc:
                    self._drop()
                    if attempt == 2:
                        raise VistaUnavailableError(f"VistA unavailable: {exc}") from exc
                    logger.warning("vista call failed, reconnecting: %s", exc)
        raise VistaUnavailableError("unreachable")  # pragma: no cover

    def _drop(self) -> None:
        broker, self._broker = self._broker, None
        if broker is not None:
            try:
                broker.disconnect()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001  # best effort close
                logger.debug("disconnect failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self._drop()


class RecordedRpcClient:
    """Replays recorded raw replies. Unknown calls raise so gaps are never silently empty."""

    def __init__(self, index_file: Path) -> None:
        self._dir = index_file.parent
        self._index: dict[str, dict[str, str]] = json.loads(index_file.read_text("utf-8"))

    def call(self, rpc: str, *params: str) -> str:
        entry = self._index.get(call_key(rpc, tuple(str(p) for p in params)))
        if entry is None:
            raise VistaUnavailableError(f"no recorded reply for {rpc}")
        return entry["reply"]

    def known_calls(self) -> list[tuple[str, tuple[str, ...]]]:
        return [(e["rpc"], tuple(json.loads(e["params"]))) for e in self._index.values()]

    def close(self) -> None:
        return None


class RecordingRpcClient:
    """Wraps a live client and records every reply (used by scripts/capture_vista_fixtures.py)."""

    def __init__(self, inner: RpcClient) -> None:
        self._inner = inner
        self.recorded: dict[str, dict[str, object]] = {}

    def call(self, rpc: str, *params: str) -> str:
        reply = self._inner.call(rpc, *params)
        self.recorded[call_key(rpc, tuple(str(p) for p in params))] = {
            "rpc": rpc,
            "params": json.dumps([str(p) for p in params]),
            "reply": reply,
        }
        return reply

    def close(self) -> None:
        self._inner.close()
