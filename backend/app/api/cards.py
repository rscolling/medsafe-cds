"""Alert -> CDS Hooks 2.0 Card conversion (https://cds-hooks.hl7.org/2.0/#card-attributes)."""

from __future__ import annotations

import copy
import uuid
from typing import Any

from app.model import Alert
from app.rules.engine import DISCLAIMER

OVERRIDE_SYSTEM = "urn:medsafe-cds:override-reasons"  # namespace identifier only, not a resolvable URL
Resource = dict[str, Any]


def _markdown_detail(alert: Alert) -> str:
    why = "\n".join(f"- {w}" for w in alert.why)
    suggestions = "\n".join(f"- {s}" for s in alert.suggestions)
    parts = [
        f"> **{DISCLAIMER}**",
        "",
        alert.detail,
        "",
        "**Why this fired**",
        why,
    ]
    if suggestions:
        parts += ["", "**Suggested alternative**", suggestions]
    parts += [
        "",
        f"**Source:** [{alert.source_label}]({alert.source_url})",
        f"**Rule:** `{alert.rule_id}` v{alert.rule_version} ({alert.mode} mode)",
        "",
        f"_{DISCLAIMER}_",
    ]
    return "\n".join(parts)


def _suggestion_actions(label: str, order: Resource | None, suggestion: Any) -> list[dict[str, Any]]:
    if order is None:
        return []
    if suggestion.delete_order:
        return [
            {
                "type": "delete",
                "description": f"Remove the draft order ({label})",
                "resourceId": f"MedicationRequest/{order.get('id', 'draft')}",
            }
        ]
    if suggestion.replace_dose_mg is not None:
        updated = copy.deepcopy(order)
        for di in updated.get("dosageInstruction", []):
            for dr in di.get("doseAndRate", []):
                q = dr.get("doseQuantity")
                if q is not None:
                    q["value"] = suggestion.replace_dose_mg
        return [{"type": "update", "description": label, "resource": updated}]
    return []


def unchecked_card(reason: str, evaluated_mode: str) -> dict[str, Any]:
    """Info card for a draft order that could not be checked: never a silent empty response."""
    return {
        "uuid": str(uuid.uuid4()),
        "summary": "Medication-safety check not performed for this order"[:139],
        "indicator": "info",
        "detail": "\n".join(
            [
                f"> **{DISCLAIMER}**",
                "",
                f"No medication-safety check was performed for this order: {reason}.",
                "",
                "Review this order manually. Absence of an alert here does not mean the order is safe.",
                "",
                f"_{DISCLAIMER}_",
            ]
        ),
        "source": {"label": "medsafe-cds prototype"},
        "extension": {
            "org.medsafe.mode": evaluated_mode,
            "org.medsafe.unchecked": True,
            "org.medsafe.dataGap": True,
            "org.medsafe.disclaimer": DISCLAIMER,
        },
    }


def alert_to_card(
    alert: Alert, order: Resource | None = None, *, as_of: str | None = None, as_of_policy: str | None = None
) -> dict[str, Any]:
    """Build a spec-conformant card. Optional fields are omitted when empty (spec: no null/empty)."""
    card: dict[str, Any] = {
        "uuid": str(uuid.uuid4()),
        "summary": alert.summary,
        "indicator": alert.indicator,
        "detail": _markdown_detail(alert),
        "source": {"label": alert.source_label, "url": alert.source_url},
        "links": [
            {
                "label": f"Public source: {alert.source_label}"[:200],
                "url": alert.source_url,
                "type": "absolute",
            }
        ],
        "extension": {
            "org.medsafe.ruleId": alert.rule_id,
            "org.medsafe.ruleVersion": alert.rule_version,
            "org.medsafe.mode": alert.mode,
            "org.medsafe.dataGap": alert.data_gap,
            "org.medsafe.why": list(alert.why),
            "org.medsafe.disclaimer": DISCLAIMER,
        },
    }
    if as_of:
        card["extension"]["org.medsafe.asOf"] = as_of
        card["extension"]["org.medsafe.asOfPolicy"] = as_of_policy or ""
        card["detail"] += f"\n\n_Lab windows measured from {as_of} (as-of policy: {as_of_policy})._"
    defs = list(alert.suggestion_defs)
    suggestions: list[dict[str, Any]] = []
    for i, label in enumerate(alert.suggestions):
        s: dict[str, Any] = {"label": label, "uuid": str(uuid.uuid4()), "isRecommended": i == 0}
        actions = _suggestion_actions(label, order, defs[i]) if i < len(defs) else []
        if actions:
            s["actions"] = actions
        suggestions.append(s)
    if suggestions:
        card["suggestions"] = suggestions
        card["selectionBehavior"] = "at-most-one"
    if alert.override_reasons:
        card["overrideReasons"] = [
            {"code": code, "system": OVERRIDE_SYSTEM, "display": display} for code, display in alert.override_reasons
        ]
    return card
