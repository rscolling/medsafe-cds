"""Runtime configuration (environment variables, 12-factor style)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# MEDSAFE_ROOT lets a non-editable install (e.g. the Docker image) point at the rules/ and data/ directories.
REPO_ROOT = Path(os.environ.get("MEDSAFE_ROOT") or Path(__file__).resolve().parents[2])


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


DEFAULT_SALT = "dev-only-salt"
VALID_AS_OF_POLICIES = ("auto", "anchored", "today")


class InsecureConfigError(RuntimeError):
    """Raised at startup when MEDSAFE_ENV=production and a required hardening setting is missing."""


@dataclass(frozen=True)
class Settings:
    """All settings are read once; tests build their own instances."""

    rules_dir: Path = field(default_factory=lambda: REPO_ROOT / "rules")
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data")

    # FHIR source: "http" (HAPI) | "fixtures" (in-repo JSON) | "auto" (try HAPI, else fixtures)
    fhir_mode: str = field(default_factory=lambda: _env("MEDSAFE_FHIR_MODE", "auto"))
    fhir_base_url: str = field(default_factory=lambda: _env("MEDSAFE_FHIR_BASE_URL", "http://localhost:8090/fhir"))
    fhir_timeout_s: float = field(default_factory=lambda: float(_env("MEDSAFE_FHIR_TIMEOUT_S", "3")))

    # VistA source: "live" (RPC Broker) | "recorded" (recorded replies) | "auto"
    vista_mode: str = field(default_factory=lambda: _env("MEDSAFE_VISTA_MODE", "auto"))
    vista_host: str = field(default_factory=lambda: _env("VISTA_HOST", "localhost"))
    vista_port: int = field(default_factory=lambda: int(_env("VISTA_PORT", "9430")))
    # Public VEHU demo codes live in .env (see .env.example); never hard-coded here.
    vista_access: str = field(default_factory=lambda: _env("VISTA_ACCESS_CODE"), repr=False)
    vista_verify: str = field(default_factory=lambda: _env("VISTA_VERIFY_CODE"), repr=False)
    vista_context: str = field(default_factory=lambda: _env("VISTA_CONTEXT", "OR CPRS GUI CHART"))
    vista_timeout_s: float = field(default_factory=lambda: float(_env("MEDSAFE_VISTA_TIMEOUT_S", "8")))
    vista_cache_ttl_s: float = field(default_factory=lambda: float(_env("MEDSAFE_VISTA_CACHE_TTL_S", "60")))
    # Total time budget for fetching one patient from VistA (a CDS hook must answer in seconds, not minutes).
    vista_fetch_deadline_s: float = field(default_factory=lambda: float(_env("MEDSAFE_VISTA_FETCH_DEADLINE_S", "10")))
    # Cool-downs (seconds): after a rejected sign-on no new sign-on is attempted (account-lockout protection);
    # after a transport failure calls fail fast instead of each paying a connect timeout.
    vista_auth_cooldown_s: float = field(default_factory=lambda: float(_env("MEDSAFE_VISTA_AUTH_COOLDOWN_S", "300")))
    vista_breaker_cooldown_s: float = field(
        default_factory=lambda: float(_env("MEDSAFE_VISTA_BREAKER_COOLDOWN_S", "15"))
    )
    # ORWPS ACTIVE statuses treated as "currently taking". VEHU outpatient orders are all PENDING, so PENDING counts by
    # default; set MEDSAFE_VISTA_PENDING_ACTIVE=false to ignore unsigned/unreleased orders.
    vista_pending_active: bool = field(
        default_factory=lambda: _env("MEDSAFE_VISTA_PENDING_ACTIVE", "true").lower() not in ("0", "false", "no")
    )

    # "dev" (default): insecure defaults allowed but logged loudly. "production": refuse to start without
    # MEDSAFE_AUDIT_API_KEY and a non-default MEDSAFE_PSEUDONYM_SALT.
    env: str = field(default_factory=lambda: _env("MEDSAFE_ENV", "dev").lower())
    max_body_bytes: int = field(default_factory=lambda: int(_env("MEDSAFE_MAX_BODY_BYTES", str(1_000_000))))
    max_draft_orders: int = field(default_factory=lambda: int(_env("MEDSAFE_MAX_DRAFT_ORDERS", "50")))

    audit_db: str = field(default_factory=lambda: _env("MEDSAFE_AUDIT_DB", "audit.sqlite3"))
    # repr=False on every secret so repr(Settings()) / a stray log line / a traceback cannot leak them.
    audit_api_key: str = field(default_factory=lambda: _env("MEDSAFE_AUDIT_API_KEY"), repr=False)
    pseudonym_salt: str = field(default_factory=lambda: _env("MEDSAFE_PSEUDONYM_SALT", DEFAULT_SALT), repr=False)

    # What "now" means for windows such as "eGFR within 90 days":
    #   today    - the wall clock (production semantics: a creatinine from 2021 is stale and yields a data-gap card)
    #   anchored - the newest observation in the patient's own record (needed for VEHU, whose data is dated 2009-2016,
    #              and for the static bundled fixtures)
    #   auto     - (default) `today` for caller-supplied prefetch data, `anchored` for the bundled/recorded sources.
    as_of_policy: str = field(default_factory=lambda: _env("MEDSAFE_AS_OF_POLICY", "auto").lower())
    # Trusted reverse proxy header carrying the real client IP (e.g. "X-Forwarded-For"); empty = use the socket peer.
    # Only set this when the proxy overwrites the header, otherwise clients can spoof it to dodge the rate limit.
    client_ip_header: str = field(default_factory=lambda: _env("MEDSAFE_CLIENT_IP_HEADER"))

    rate_limit_per_minute: int = field(default_factory=lambda: int(_env("MEDSAFE_RATE_LIMIT_PER_MINUTE", "120")))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o for o in _env("MEDSAFE_CORS_ORIGINS", "http://localhost:5173,http://localhost:8080").split(",") if o
        )
    )
    log_level: str = field(default_factory=lambda: _env("MEDSAFE_LOG_LEVEL", "INFO"))
    # Exposes a CSV of VEHU patients that must never be used in demos (junk 1,430-order patient).
    vista_excluded_dfns: tuple[str, ...] = ("100897",)

    def security_findings(self) -> list[str]:
        """Human-readable list of insecure settings (empty when hardened)."""
        findings: list[str] = []
        if self.as_of_policy not in VALID_AS_OF_POLICIES:
            findings.append(f"MEDSAFE_AS_OF_POLICY must be one of {VALID_AS_OF_POLICIES}, got {self.as_of_policy!r}")
        if not self.audit_api_key:
            findings.append(
                "MEDSAFE_AUDIT_API_KEY is not set: /api/audit, /api/audit/summary, /api/sources, /api/patients, "
                "/api/compare, /metrics, /docs and /openapi.json are open to anyone who can reach this port"
            )
        if self.pseudonym_salt == DEFAULT_SALT or len(self.pseudonym_salt) < 16:
            findings.append(
                "MEDSAFE_PSEUDONYM_SALT is the built-in default (or shorter than 16 chars): pseudonymised ids in "
                "logs/audit can be reversed by brute force"
            )
        return findings

    def enforce_security(self, log: logging.Logger) -> None:
        """production: raise on insecure settings; dev: log them loudly. Never a silent pass."""
        findings = self.security_findings()
        if not findings:
            return
        if self.env == "production":
            raise InsecureConfigError("refusing to start with MEDSAFE_ENV=production: " + "; ".join(findings))
        for f in findings:
            log.warning("INSECURE DEV DEFAULT (do not expose this service beyond localhost): %s", f)
