"""Cohort comparison: baseline (drug-class match) vs context-aware alert counts.

Everything printed is labeled "illustrative on synthetic data". The numbers depend on how the
synthetic cohort was generated and on the author-assigned ``clinically_warranted`` labels of the
hand-authored scenarios; they are NOT evidence of real-world performance.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.cohort import CANDIDATE_ORDERS, med_resource
from app.service import CdsService

LABEL = "ILLUSTRATIVE ON SYNTHETIC DATA - prototype, not clinical advice"


@dataclass
class Tally:
    evaluations: int = 0
    baseline_alerts: int = 0
    context_fire: int = 0
    context_data_gap: int = 0
    per_rule: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(Counter))

    def add(self, rule_id: str, key: str, n: int = 1) -> None:
        self.per_rule[rule_id][key] += n

    @property
    def suppressed(self) -> int:
        return self.baseline_alerts - self.context_fire - self.context_data_gap

    @property
    def reduction_pct(self) -> float:
        return 0.0 if not self.baseline_alerts else round(100.0 * self.suppressed / self.baseline_alerts, 1)


def run_cohort(service: CdsService, source: str, patient_ids: list[str], order_keys: list[str] | None = None) -> Tally:
    """Try each candidate draft order for every patient in both modes and count the alerts."""
    tally = Tally()
    for pid in patient_ids:
        try:
            ctx = service.load_context(source, pid)
        except Exception:  # noqa: BLE001, S112  # unreachable/unmappable patient: skip
            continue
        for key in order_keys or CANDIDATE_ORDERS:
            draft = med_resource(pid, key, status="draft", idx=99)
            base = service.evaluate_ctx(ctx, draft, "baseline").result
            cont = service.evaluate_ctx(ctx, draft, "context").result
            tally.evaluations += 1
            tally.baseline_alerts += len(base.alerts)
            for a in base.alerts:
                tally.add(a.rule_id, "baseline")
            for a in cont.alerts:
                if a.data_gap:
                    tally.context_data_gap += 1
                    tally.add(a.rule_id, "context_data_gap")
                else:
                    tally.context_fire += 1
                    tally.add(a.rule_id, "context_fire")
    return tally


def labeled_scenarios(
    service: CdsService, source: str, docs_dir: Path, twin_map: dict[str, str] | None = None
) -> dict[str, Any]:
    """Score both modes against the hand-authored labels (``clinically_warranted``).

    A "warranted" alert is one the author judged appropriate to show for that scenario. Data-gap
    cards count as warranted only when the scenario is labeled so (missing renal function).
    """
    tp = {"baseline": 0, "context": 0}
    fp = {"baseline": 0, "context": 0}
    fn = {"baseline": 0, "context": 0}
    rows: list[dict[str, Any]] = []
    for f in sorted(docs_dir.glob("*.json")):
        doc = json.loads(f.read_text("utf-8"))
        pid = (twin_map or {}).get(doc["id"], doc["id"])
        ctx = service.load_context(source, pid)
        for sc in doc["scenarios"]:
            warranted = set(sc["expect_context"]) if sc["clinically_warranted"] else set()
            for mode in ("baseline", "context"):
                got = {a.rule_id for a in service.evaluate_ctx(ctx, sc["order"], mode).result.alerts}
                tp[mode] += len(got & warranted)
                fp[mode] += len(got - warranted)
                fn[mode] += len(warranted - got)
                rows.append(
                    {
                        "patient": doc["id"],
                        "order": sc["order_key"],
                        "mode": mode,
                        "alerts": sorted(got),
                    }
                )

    def prf(m: str) -> dict[str, float | int]:
        p = tp[m] / (tp[m] + fp[m]) if tp[m] + fp[m] else 0.0
        r = tp[m] / (tp[m] + fn[m]) if tp[m] + fn[m] else 0.0
        return {
            "tp": tp[m],
            "fp": fp[m],
            "fn": fn[m],
            "precision": round(p, 3),
            "recall": round(r, 3),
        }

    return {"baseline": prf("baseline"), "context": prf("context"), "rows": rows}


def format_report(sections: list[tuple[str, Tally]], labeled: dict[str, dict[str, Any]], notes: list[str]) -> str:
    out = [f"=== {LABEL} ===", ""]
    for title, t in sections:
        out += [
            f"## {title}",
            f"evaluations (patient x candidate order): {t.evaluations}",
            f"baseline alerts:                   {t.baseline_alerts}",
            f"context-aware actionable alerts:   {t.context_fire}",
            f"context-aware data-gap (info):     {t.context_data_gap}",
            f"suppressed by context:             {t.suppressed} ({t.reduction_pct}% of baseline)",
            "",
            f"  {'rule':34s} {'baseline':>9s} {'fire':>6s} {'gap':>5s}",
        ]
        for rid, c in sorted(t.per_rule.items()):
            out.append(f"  {rid:34s} {c['baseline']:>9d} {c['context_fire']:>6d} {c['context_data_gap']:>5d}")
        out.append("")
    for title, res in labeled.items():
        b, c = res["baseline"], res["context"]
        out += [
            f"## Hand-authored labeled scenarios ({title})",
            f"baseline: TP={b['tp']} FP={b['fp']} FN={b['fn']} precision={b['precision']} recall={b['recall']}",
            f"context : TP={c['tp']} FP={c['fp']} FN={c['fn']} precision={c['precision']} recall={c['recall']}",
            "",
        ]
    out += [
        "## Limits (read before quoting any number)",
        *[f"- {n}" for n in notes],
        "",
        f"=== {LABEL} ===",
    ]
    return "\n".join(out)


LIMIT_NOTES = [
    "Cohorts are synthetic. The Synthea-style cohort is drawn from a seeded generator whose prevalence and lab distributions were chosen by the author; the reduction % mostly reflects those choices.",
    "The VEHU cohort is a public test database with 2009-2016 dates, stock copied lab values, and few relevant drug combinations; most candidate orders find no matching class there.",
    "'Clinically warranted' labels are the author's judgement on 10 hand-authored scenarios (n is tiny); precision/recall there show the mechanism, not accuracy.",
    "Context mode adds data-gap info cards instead of silently passing; they are counted separately from actionable alerts.",
    "No clinician review, no real EHR data, no outcome data. Rule thresholds are prototype readings of public labels/literature, not clinical guidance.",
]
