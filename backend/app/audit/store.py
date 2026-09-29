"""Audit trail of alerts shown and clinician overrides (SQLite, append-only by convention).

Stores pseudonymised patient ids, rule id/version, mode, source, card uuid, override reason codes
and *hash* of free-text comments only... plus the comment length. Free text can contain PHI in a
real deployment, so it is never persisted. Synthetic data only in this prototype.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,            -- alert_shown | feedback | source_error
  request_id TEXT,
  hook_instance TEXT,
  patient_pseudo TEXT,
  source TEXT,
  mode TEXT,
  rule_id TEXT,
  rule_version TEXT,
  card_uuid TEXT,
  outcome TEXT,                  -- accepted | overridden
  override_code TEXT,
  comment_sha256 TEXT,
  comment_len INTEGER,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_events(kind);
CREATE INDEX IF NOT EXISTS idx_audit_card ON audit_events(card_uuid);
"""
_COLUMN_ORDER = (
    "ts",
    "kind",
    "request_id",
    "hook_instance",
    "patient_pseudo",
    "source",
    "mode",
    "rule_id",
    "rule_version",
    "card_uuid",
    "outcome",
    "override_code",
    "comment_sha256",
    "comment_len",
    "detail",
)
_INSERT = (
    "INSERT INTO audit_events (ts, kind, request_id, hook_instance, patient_pseudo, source, mode, rule_id, "
    "rule_version, card_uuid, outcome, override_code, comment_sha256, comment_len, detail) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_COLUMNS = frozenset(
    {
        "ts",
        "kind",
        "request_id",
        "hook_instance",
        "patient_pseudo",
        "source",
        "mode",
        "rule_id",
        "rule_version",
        "card_uuid",
        "outcome",
        "override_code",
        "comment_sha256",
        "comment_len",
        "detail",
    }
)


class AuditStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _insert(self, **cols: Any) -> None:
        unknown = set(cols) - _COLUMNS
        if unknown:
            raise ValueError(f"unknown audit column(s): {sorted(unknown)}")
        cols.setdefault("ts", datetime.now(UTC).isoformat(timespec="milliseconds"))
        with self._lock:
            self._conn.execute(_INSERT, tuple(cols.get(c) for c in _COLUMN_ORDER))
            self._conn.commit()

    def record_alert(
        self,
        *,
        request_id: str,
        hook_instance: str | None,
        patient_pseudo: str,
        source: str,
        mode: str,
        rule_id: str,
        rule_version: str,
        card_uuid: str,
        data_gap: bool,
    ) -> None:
        self._insert(
            kind="alert_shown",
            request_id=request_id,
            hook_instance=hook_instance,
            patient_pseudo=patient_pseudo,
            source=source,
            mode=mode,
            rule_id=rule_id,
            rule_version=rule_version,
            card_uuid=card_uuid,
            detail=json.dumps({"data_gap": data_gap}),
        )

    def record_feedback(
        self,
        *,
        request_id: str,
        card_uuid: str,
        outcome: str,
        override_code: str | None,
        comment: str | None,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT rule_id, rule_version, patient_pseudo, source, mode, hook_instance FROM audit_events "
                "WHERE kind='alert_shown' AND card_uuid=? LIMIT 1",
                (card_uuid,),
            ).fetchone()
        base = dict(row) if row else {}
        self._insert(
            kind="feedback",
            request_id=request_id,
            card_uuid=card_uuid,
            outcome=outcome,
            override_code=override_code,
            comment_sha256=hashlib.sha256(comment.encode()).hexdigest() if comment else None,
            comment_len=len(comment) if comment else None,
            **base,
        )

    def record_source_error(self, *, request_id: str, source: str, error: str) -> None:
        self._insert(kind="source_error", request_id=request_id, source=source, detail=error[:500])

    def query(self, kind: str | None = None, rule_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        args: list[str | int] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if rule_id:
            sql += " AND rule_id=?"
            args.append(rule_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(limit, 1000)))  # a negative LIMIT means 'all rows' in SQLite
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            shown = self._conn.execute(
                "SELECT rule_id, mode, COUNT(*) n FROM audit_events WHERE kind='alert_shown' GROUP BY 1,2"
            ).fetchall()
            overrides = self._conn.execute(
                "SELECT rule_id, override_code, COUNT(*) n FROM audit_events WHERE kind='feedback' AND outcome='overridden' GROUP BY 1,2"
            ).fetchall()
            accepted = self._conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE kind='feedback' AND outcome='accepted'"
            ).fetchone()[0]
        return {
            "alerts_shown": [dict(r) for r in shown],
            "overrides": [dict(r) for r in overrides],
            "accepted": accepted,
        }

    def ping(self) -> bool:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()
        return True
