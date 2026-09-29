#!/usr/bin/env python3
"""Count alerts over the synthetic cohorts: naive baseline vs context-aware.

Usage: python scripts/baseline_comparison.py [--json out.json] [--cohort-size 300]
Output is labeled "illustrative on synthetic data". Data sources are chosen like the service does
(env: MEDSAFE_FHIR_MODE / MEDSAFE_VISTA_MODE); by default in-repo fixtures + recorded VEHU replies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "third_party"))
os.environ.setdefault("MEDSAFE_FHIR_MODE", "fixtures")
os.environ.setdefault("MEDSAFE_VISTA_MODE", "recorded")

from app.comparison import LIMIT_NOTES, format_report, labeled_scenarios, run_cohort  # noqa: E402
from app.config import Settings  # noqa: E402
from app.registry import build_fhir, build_vista  # noqa: E402
from app.rules.engine import RulesEngine  # noqa: E402
from app.service import CdsService  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--cohort-size", type=int, default=300)
    args = ap.parse_args()
    settings = Settings()
    engine = RulesEngine.from_dir(settings.rules_dir)
    fhir, vista = build_fhir(settings, args.cohort_size), build_vista(settings)
    service = CdsService(engine, {"fhir": fhir, "vista": vista})

    syn_ids = sorted(d for d in fhir._store.docs if d.startswith("syn-"))  # noqa: SLF001
    hand_ids = sorted(d for d in fhir._store.docs if d.startswith("hand-"))  # noqa: SLF001
    vehu_ids = [p.id for p in vista.list_patients() if p.synthetic_kind == "vehu"]
    twin_ids = [p.id for p in vista.list_patients() if p.synthetic_kind == "vehu-synthetic-overlay"]

    sections = [
        (f"FHIR: Synthea-style synthetic cohort (n={len(syn_ids)})", run_cohort(service, "fhir", syn_ids)),
        (f"FHIR: hand-authored patients (n={len(hand_ids)})", run_cohort(service, "fhir", hand_ids)),
        (f"VistA ({vista.mode}): VEHU patients with meds (n={len(vehu_ids)})", run_cohort(service, "vista", vehu_ids)),
        (f"VistA: synthetic overlay twins (n={len(twin_ids)})", run_cohort(service, "vista", twin_ids)),
    ]
    docs_dir = settings.data_dir / "patients" / "hand-authored"
    twin_map = {f"hand-{i:02d}": str(9000000 + i) for i in range(1, 11)}
    labeled = {
        "FHIR": labeled_scenarios(service, "fhir", docs_dir),
        "VistA twins": labeled_scenarios(service, "vista", docs_dir, twin_map),
    }
    print(format_report(sections, labeled, LIMIT_NOTES))
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "label": "illustrative on synthetic data",
                    "sections": {
                        t: {
                            "evaluations": s.evaluations,
                            "baseline": s.baseline_alerts,
                            "context_fire": s.context_fire,
                            "context_data_gap": s.context_data_gap,
                            "suppressed": s.suppressed,
                            "reduction_pct": s.reduction_pct,
                            "per_rule": {k: dict(v) for k, v in s.per_rule.items()},
                        }
                        for t, s in sections
                    },
                    "labeled": {k: {"baseline": v["baseline"], "context": v["context"]} for k, v in labeled.items()},
                    "limits": LIMIT_NOTES,
                },
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
