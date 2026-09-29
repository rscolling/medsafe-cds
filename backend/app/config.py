"""Runtime configuration (environment variables, 12-factor style)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


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
    vista_access: str = field(default_factory=lambda: _env("VISTA_ACCESS_CODE"))
    vista_verify: str = field(default_factory=lambda: _env("VISTA_VERIFY_CODE"))
    vista_context: str = field(default_factory=lambda: _env("VISTA_CONTEXT", "OR CPRS GUI CHART"))
    vista_timeout_s: float = field(default_factory=lambda: float(_env("MEDSAFE_VISTA_TIMEOUT_S", "8")))
    vista_cache_ttl_s: float = field(default_factory=lambda: float(_env("MEDSAFE_VISTA_CACHE_TTL_S", "60")))

    audit_db: str = field(default_factory=lambda: _env("MEDSAFE_AUDIT_DB", "audit.sqlite3"))
    audit_api_key: str = field(default_factory=lambda: _env("MEDSAFE_AUDIT_API_KEY"))
    pseudonym_salt: str = field(default_factory=lambda: _env("MEDSAFE_PSEUDONYM_SALT", "dev-only-salt"))

    rate_limit_per_minute: int = field(default_factory=lambda: int(_env("MEDSAFE_RATE_LIMIT_PER_MINUTE", "120")))
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o for o in _env("MEDSAFE_CORS_ORIGINS", "http://localhost:5173,http://localhost:8080").split(",") if o
        )
    )
    log_level: str = field(default_factory=lambda: _env("MEDSAFE_LOG_LEVEL", "INFO"))
    # Exposes a CSV of VEHU patients that must never be used in demos (junk 1,430-order patient).
    vista_excluded_dfns: tuple[str, ...] = ("100897",)
