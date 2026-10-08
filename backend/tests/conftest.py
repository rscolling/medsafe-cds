from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("MEDSAFE_LOG_LEVEL", "WARNING")

from app.audit.store import AuditStore  # noqa: E402
from app.config import Settings  # noqa: E402
from app.registry import build_fhir, build_vista  # noqa: E402
from app.rules.engine import RulesEngine  # noqa: E402
from app.service import CdsService  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
HAND_DIR = ROOT / "data" / "patients" / "hand-authored"
# hand-authored FHIR id -> its VistA overlay twin's DFN (from the generated overlay, so new patients are picked up)
TWIN = {
    p["twin_of"]: p["dfn"]
    for p in json.loads((ROOT / "data" / "vista" / "overlay_patients.json").read_text())["patients"]
    if p.get("twin_of")
}


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(fhir_mode="fixtures", vista_mode="recorded", audit_db=":memory:")


@pytest.fixture(scope="session")
def engine(settings: Settings) -> RulesEngine:
    return RulesEngine.from_dir(settings.rules_dir)


@pytest.fixture(scope="session")
def fhir(settings: Settings):  # type: ignore[no-untyped-def]
    return build_fhir(settings, cohort_size=40)


@pytest.fixture(scope="session")
def vista(settings: Settings):  # type: ignore[no-untyped-def]
    return build_vista(settings)


@pytest.fixture(scope="session")
def service(engine: RulesEngine, fhir, vista) -> CdsService:  # type: ignore[no-untyped-def]
    return CdsService(engine, {"fhir": fhir, "vista": vista})


@pytest.fixture
def audit() -> AuditStore:
    return AuditStore(":memory:")


def load_hand_docs() -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(p.read_text()) for p in sorted(HAND_DIR.glob("*.json"))]


def all_scenarios() -> list[tuple[str, str, dict, dict]]:  # type: ignore[type-arg]
    return [(d["id"], sc["order_key"], d, sc) for d in load_hand_docs() for sc in d["scenarios"]]


@pytest.fixture(scope="session")
def vehu_real() -> dict[str, str]:
    return json.loads((Path(__file__).parent / "fixtures" / "vehu_real_replies.json").read_text())
