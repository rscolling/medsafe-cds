"""Rule loader and evaluator (baseline vs context-aware).

*Baseline* answers "does the regimen match the drug-class pattern?" - the classic drug-class alert.
*Context-aware* additionally runs the rule's named check / declarative suppression against the
patient's labs, conditions and age and either fires (with the concrete numbers), stays quiet
(recording why in ``suppressed``), or emits an explicit *data gap* card - never a silent pass.
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from app.model import Alert, DrugRef, EvaluationResult, PatientContext, Suppressed
from app.rules import checks
from app.rules.schema import CardText, Predicate, Rule

logger = logging.getLogger("medsafe.rules")

DISCLAIMER = "Prototype, not clinical advice. Synthetic data only."
_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
MODES = ("baseline", "context")


class RuleLoadError(ValueError):
    pass


def load_rules(rules_dir: Path) -> list[Rule]:
    rules: list[Rule] = []
    seen: set[str] = set()
    for path in sorted(rules_dir.glob("*.yaml")):
        try:
            rule = Rule.model_validate(yaml.safe_load(path.read_text("utf-8")))
        except Exception as exc:
            raise RuleLoadError(f"{path.name}: {exc}") from exc
        if rule.id in seen:
            raise RuleLoadError(f"duplicate rule id {rule.id}")
        if rule.context.check and rule.context.check not in checks.REGISTRY:
            raise RuleLoadError(f"{rule.id}: unknown check {rule.context.check!r}")
        seen.add(rule.id)
        rules.append(rule)
    if not rules:
        raise RuleLoadError(f"no rules found in {rules_dir}")
    return rules


def _assign_groups(regimen: list[DrugRef], order: DrugRef, groups: list[list[str]]) -> list[DrugRef] | None:
    """Pick a distinct drug for each group with the draft order occupying one of them.

    Small backtracking search (groups <= 3). Returns drugs aligned with ``groups`` or None.
    """
    candidates: list[list[DrugRef]] = [[d for d in regimen if d.mapped and d.classes & set(g)] for g in groups]

    def solve(i: int, used: tuple[int, ...], chosen: list[DrugRef]) -> list[DrugRef] | None:
        if i == len(groups):
            return chosen if any(d is order for d in chosen) else None
        for d in candidates[i]:
            if id(d) in used:
                continue
            got = solve(i + 1, (*used, id(d)), [*chosen, d])
            if got:
                return got
        return None

    return solve(0, (), [])


def _fmt(template: str, facts: dict[str, Any]) -> str:
    try:
        return template.format(**facts)
    except (KeyError, IndexError, ValueError) as exc:
        raise RuleLoadError(f"card template {template!r} needs {exc}") from exc


class RulesEngine:
    def __init__(self, rules: Iterable[Rule]) -> None:
        self.rules = list(rules)

    @classmethod
    def from_dir(cls, rules_dir: Path) -> RulesEngine:
        return cls(load_rules(rules_dir))

    def catalog(self) -> list[dict[str, str]]:
        return [{"id": r.id, "version": r.version, "title": r.title, "source": r.card.source.url} for r in self.rules]

    # ------------------------------------------------------------------ public
    def evaluate(self, ctx: PatientContext, order: DrugRef, mode: str) -> EvaluationResult:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        regimen = [*ctx.meds, order]
        alerts: list[Alert] = []
        suppressed: list[Suppressed] = []
        for rule in self.rules:
            drugs = _assign_groups(regimen, order, rule.match.groups)
            if drugs is None:
                continue
            base_facts = self._base_facts(regimen, order, drugs, rule)
            if mode == "baseline":
                alerts.append(self._alert(rule, rule.card.fire, base_facts, "baseline", base_facts, {}))
                continue
            outcome = self._context_outcome(rule, ctx, order, regimen, base_facts)
            if outcome.status == "quiet":
                suppressed.append(Suppressed(rule.id, rule.version, tuple(outcome.reasons)))
            elif outcome.status == "data_gap":
                gap = rule.card.data_gap
                if gap is None:  # rule declares no gap card: treat as unable to assess -> fire
                    alerts.append(
                        self._alert(
                            rule,
                            rule.card.fire,
                            base_facts,
                            "context",
                            {**base_facts, **outcome.facts},
                            {},
                        )
                    )
                else:
                    alerts.append(
                        self._alert(
                            rule,
                            gap,
                            base_facts,
                            "context",
                            {**base_facts, **outcome.facts},
                            {"data_gap": True},
                        )
                    )
            else:
                alerts.append(
                    self._alert(
                        rule,
                        rule.card.fire,
                        base_facts,
                        "context",
                        {**base_facts, **outcome.facts},
                        {"indicator": outcome.indicator},
                    )
                )
        return EvaluationResult(mode, tuple(alerts), tuple(suppressed))

    # ------------------------------------------------------------------ internals
    def _base_facts(self, regimen: list[DrugRef], order: DrugRef, drugs: list[DrugRef], rule: Rule) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "order": order.raw,
            "regimen": ", ".join(sorted({d.name for d in regimen if d.mapped})) or "none",
            "window_days": rule.context.params.get("window_days", ""),
        }
        for i, d in enumerate(drugs):
            facts[f"group{i + 1}"] = d.name
        # friendly names for known rule shapes
        names = {g[0]: d.name for g, d in zip(rule.match.groups, drugs, strict=True)}
        for cls_name, drug_name in names.items():
            facts[cls_name] = drug_name
        facts.setdefault("dose", "unknown")
        return facts

    def _context_outcome(
        self,
        rule: Rule,
        ctx: PatientContext,
        order: DrugRef,
        regimen: list[DrugRef],
        facts: dict[str, Any],
    ) -> checks.CheckOutcome:
        outcome = checks.CheckOutcome("fire", dict(facts))
        if rule.context.check:
            fn = checks.REGISTRY[rule.context.check]
            outcome = fn(ctx, order, regimen, rule.context.params)
            if outcome.status != "fire":
                return outcome
        if rule.context.suppress_when_all:
            unmet: list[str] = []
            met_reasons: list[str] = []
            for p in rule.context.suppress_when_all:
                ok, text = _predicate(ctx, p)
                (met_reasons if ok else unmet).append(text)
            if not unmet:
                return checks.CheckOutcome("quiet", outcome.facts, met_reasons)
            outcome.facts["unmet"] = "; ".join(unmet)
        return outcome

    def _alert(
        self,
        rule: Rule,
        text: CardText,
        base_facts: dict[str, Any],
        mode: str,
        facts: dict[str, Any],
        extra: dict[str, Any],
    ) -> Alert:
        merged = {**base_facts, **facts}
        merged.setdefault("unmet", "no recent potassium/eGFR context evaluated (baseline mode)")
        merged.setdefault("extra", "")
        if mode == "baseline":
            summary = _fmt_baseline_summary(rule)
            detail = (
                f"Baseline mode: fires on drug-class match only, without checking labs or conditions. "
                f"Rule: {rule.title}."
            )
            why: tuple[str, ...] = (
                f"Regimen after order: {merged['regimen']}.",
                "The drug classes required by this rule are all present (baseline: class match only).",
            )
            indicator = "warning"
        else:
            summary = _fmt(text.summary, merged)
            detail = _fmt(text.detail, merged)
            why = tuple(_fmt(w, merged) for w in text.why)
            indicator = extra.get("indicator") or text.indicator
        return Alert(
            rule_id=rule.id,
            rule_version=rule.version,
            mode=mode,
            indicator=indicator,
            summary=truncate_summary(summary),
            detail=detail,
            why=why,
            source_label=rule.card.source.label,
            source_url=rule.card.source.url,
            suggestions=tuple(s.label for s in text.suggestions),
            override_reasons=tuple(text.override_reasons),
            suggestion_defs=tuple(text.suggestions),
            data_gap=bool(extra.get("data_gap", False)),
            facts={k: v for k, v in merged.items() if isinstance(v, str | int | float)},
        )


def truncate_summary(text: str, limit: int = 140) -> str:
    """CDS Hooks summaries are <= 140 chars: cut at a word boundary and mark the cut with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-\u2013")
    return (cut or text[: limit - 1]) + "\u2026"


def _fmt_baseline_summary(rule: Rule) -> str:
    return f"{rule.title} (drug-class match)"


def _predicate(ctx: PatientContext, p: Predicate) -> tuple[bool, str]:
    lab = ctx.latest_lab(p.lab, p.within_days)
    if lab is None:
        return False, f"no {p.lab} within {p.within_days} days"
    ok = _OPS[p.op](lab.value, p.value)
    text = f"{p.lab} {checks.num(lab.value)} on {lab.when} (need {p.op} {p.value:g} within {p.within_days} days)"
    return ok, text
