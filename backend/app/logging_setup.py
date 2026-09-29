"""Structured (JSON) logging with request-id correlation. No PHI: patient ids are pseudonymised."""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our request log line
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def pseudonymize(value: str, salt: str) -> str:
    """Stable, non-reversible id for logs/audit (still synthetic data, but habits matter)."""
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:12]


_DIGITS = re.compile(r"\d{5,}")


def redact_error(exc: BaseException, *identifiers: str) -> str:
    """Error text that is safe for logs and the audit table.

    Exception messages can carry patient ids (DFNs, FHIR ids) or RPC parameters. Known identifiers are
    replaced by ``<id>``, any run of 5+ digits by ``<n>``, and the result is length-capped. The exception
    class name is always kept so the failure stays diagnosable.
    """
    text = str(exc)
    for ident in sorted({i for i in identifiers if i}, key=len, reverse=True):
        text = text.replace(ident, "<id>")
    text = _DIGITS.sub("<n>", text)
    return f"{type(exc).__name__}: {text}"[:200]


def short_hash(value: str) -> str:
    """Non-reversible short fingerprint for free text (order names) that should not appear in logs."""
    return hashlib.sha256(value.encode()).hexdigest()[:10]
