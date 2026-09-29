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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("medsafe.vista.rpc")


class VistaUnavailableError(RuntimeError):
    """The VistA source could not be reached or answered with a transport error."""


class VistaAuthError(VistaUnavailableError):
    """Sign-on, handshake or application-context was rejected. Never retried automatically (lockout risk)."""


class VistaCircuitOpenError(VistaUnavailableError):
    """Fast-fail: the broker failed recently, so no new connection is attempted until the cool-down ends."""


class RpcClient(Protocol):
    """Send one RPC with literal string params; return the raw reply (CRLF separated)."""

    def call(self, rpc: str, *params: str) -> str: ...

    def close(self) -> None: ...


def call_key(rpc: str, params: tuple[str, ...]) -> str:
    """Stable key for a recorded call (also used as fixture file name)."""
    digest = hashlib.sha1("\x1f".join((rpc, *params)).encode(), usedforsecurity=False).hexdigest()
    return digest[:16]


class BrokerRpcClient:
    """Live RPC Broker client: one locked connection, lazily (re)connected, with a circuit breaker.

    Failure policy (review M4/M5):

    * **Authentication / handshake / context errors are never retried.** Each sign-on attempt with a bad
      verify code counts toward the VistA lockout threshold, so the first rejection blocks all further
      sign-on attempts for ``auth_cooldown_s`` (fast-fail, no network traffic) instead of hammering the account.
    * **Connect failures are not retried inside a call.** Only a *stale established* connection (the server
      dropped it while idle) gets exactly one transparent reconnect.
    * After any transport failure the **circuit opens** for ``breaker_cooldown_s``: calls fail immediately with
      :class:`VistaCircuitOpenError` so a dead VistA costs microseconds per order-sign, not seconds. The first
      call after the cool-down is the probe (half-open).
    * Callers queue on the connection lock for at most ``lock_wait_s``; a slow broker cannot stack up threads.
    """

    def __init__(
        self,
        host: str,
        port: int,
        access: str,
        verify: str,
        context: str,
        timeout_s: float = 8.0,
        *,
        auth_cooldown_s: float = 300.0,
        breaker_cooldown_s: float = 15.0,
        lock_wait_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host, self._port = host, port
        self._access, self._verify, self._context = access, verify, context
        self._timeout = timeout_s
        self._auth_cooldown = auth_cooldown_s
        self._breaker_cooldown = breaker_cooldown_s
        self._lock_wait = timeout_s if lock_wait_s is None else lock_wait_s
        self._clock = clock
        self._lock = threading.Lock()
        self._broker: object | None = None
        self._auth_blocked_until = 0.0
        self._auth_error = ""
        self._open_until = 0.0
        self._last_error = ""
        self._last_ok: float | None = None
        self._signon_attempts = 0

    # ------------------------------------------------------------ introspection
    @property
    def signon_attempts(self) -> int:
        """Number of sign-on (connect+authenticate) attempts made so far (lockout-risk metric)."""
        return self._signon_attempts

    def health(self) -> dict[str, object]:
        """Non-blocking snapshot of the broker state; performs no network I/O and takes no lock."""
        now = self._clock()
        if now < self._auth_blocked_until:
            state = "auth-blocked"
        elif now < self._open_until:
            state = "circuit-open"
        elif self._broker is not None:
            state = "connected"
        else:
            state = "idle"
        return {
            "state": state,
            "reachable": state in ("connected", "idle") and not self._last_error,
            "last_error": self._auth_error if state == "auth-blocked" else self._last_error,
            "last_ok_age_s": None if self._last_ok is None else round(now - self._last_ok, 1),
            "signon_attempts": self._signon_attempts,
        }

    # ------------------------------------------------------------ connection
    def _connect(self) -> object:
        from vista_clients.rpc import VistABroker  # vendored, see backend/third_party

        self._signon_attempts += 1
        broker = VistABroker(self._host, self._port, timeout=self._timeout, app_name="medsafe-cds")
        try:
            broker.connect()
            broker.authenticate(self._access, self._verify)
            broker.create_context(self._context)
        except BaseException:
            try:
                broker.disconnect()
            except Exception:  # noqa: BLE001  # best effort close of a half-open session
                logger.debug("disconnect after failed sign-on failed", exc_info=True)
            raise
        logger.info("vista broker connected", extra={"host": self._host, "port": self._port})
        return broker

    def _fail(self, exc: BaseException) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"[:300]
        self._open_until = self._clock() + self._breaker_cooldown

    def call(self, rpc: str, *params: str) -> str:
        from vista_clients.rpc import literal
        from vista_clients.rpc.errors import AuthenticationError, ContextError, HandshakeError, RPCError, VistAError

        now = self._clock()
        if now < self._auth_blocked_until:
            raise VistaCircuitOpenError(
                f"VistA sign-on blocked for {self._auth_blocked_until - now:.0f}s after a rejected login "
                f"({self._auth_error}); not retrying to avoid an account lockout"
            )
        if now < self._open_until:
            raise VistaCircuitOpenError(f"VistA circuit open for {self._open_until - now:.1f}s: {self._last_error}")
        if not self._lock.acquire(timeout=self._lock_wait):
            raise VistaUnavailableError(f"VistA busy: no broker connection free within {self._lock_wait:.1f}s")
        try:
            reused = self._broker is not None
            for attempt in (1, 2):
                try:
                    if self._broker is None:
                        reused = False
                        self._broker = self._connect()
                    resp = self._broker.call_rpc(rpc, [literal(str(p)) for p in params])  # type: ignore[attr-defined]
                    self._last_ok, self._last_error = self._clock(), ""
                    return str(resp.raw)
                except RPCError as exc:  # application-level error: connection still good
                    raise VistaUnavailableError(f"RPC {rpc} failed: {exc}") from exc
                except (AuthenticationError, HandshakeError, ContextError) as exc:
                    self._drop()
                    self._auth_error = f"{type(exc).__name__}: {exc}"[:300]
                    self._auth_blocked_until = self._clock() + self._auth_cooldown
                    self._fail(exc)
                    raise VistaAuthError(f"VistA sign-on rejected ({type(exc).__name__}); retries suspended") from exc
                except (VistAError, OSError) as exc:
                    self._drop()
                    self._fail(exc)
                    # Only a stale, previously-good connection earns one transparent reconnect.
                    if attempt == 2 or not reused:
                        raise VistaUnavailableError(f"VistA unavailable: {exc}") from exc
                    self._open_until = 0.0
                    reused = False
                    logger.warning("vista connection went stale, reconnecting once: %s", exc)
            raise VistaUnavailableError("unreachable")  # pragma: no cover
        finally:
            self._lock.release()

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
