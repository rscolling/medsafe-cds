from __future__ import annotations

from app.comparison import LABEL, LIMIT_NOTES, Tally, format_report, labeled_scenarios, run_cohort
from tests.conftest import HAND_DIR, TWIN


def test_cohort_counts_and_invariants(service, fhir) -> None:  # type: ignore[no-untyped-def]
    ids = sorted(d for d in fhir._store.docs if d.startswith("syn-"))[:25]
    t = run_cohort(service, "fhir", ids)
    assert t.evaluations == 25 * 8
    assert t.baseline_alerts >= t.context_fire  # context never invents alerts a baseline lacks
    assert t.suppressed >= 0 and 0 <= t.reduction_pct <= 100
    assert sum(c["baseline"] for c in t.per_rule.values()) == t.baseline_alerts


def test_unreachable_patients_are_skipped(service) -> None:  # type: ignore[no-untyped-def]
    assert run_cohort(service, "fhir", ["nope"]).evaluations == 0


def test_labeled_scenarios_context_improves_precision_without_losing_recall(service) -> None:  # type: ignore[no-untyped-def]
    for source, twin in (("fhir", None), ("vista", TWIN)):
        res = labeled_scenarios(service, source, HAND_DIR, twin)
        assert res["context"]["precision"] > res["baseline"]["precision"]
        assert res["context"]["recall"] >= res["baseline"]["recall"]
        assert res["context"]["fp"] == 0


def test_fhir_and_vista_twin_counts_are_identical(service) -> None:  # type: ignore[no-untyped-def]
    a = labeled_scenarios(service, "fhir", HAND_DIR)
    b = labeled_scenarios(service, "vista", HAND_DIR, TWIN)
    assert a["baseline"] == b["baseline"] and a["context"] == b["context"]


def test_report_is_labeled_and_states_limits() -> None:
    t = Tally(evaluations=1, baseline_alerts=4, context_fire=1, context_data_gap=1)
    t.add("r", "baseline", 4)
    rep = format_report(
        [("x", t)],
        {
            "F": {
                "baseline": {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0},
                "context": {"tp": 1, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0},
            }
        },
        LIMIT_NOTES,
    )
    assert rep.startswith(f"=== {LABEL}") and "illustrative on synthetic data".lower() in rep.lower()
    assert "Limits" in rep and "suppressed by context:             2 (50.0% of baseline)" in rep
    assert Tally().reduction_pct == 0.0
