"""Pydantic schema for rule YAML files (validated at load; bad rules fail fast)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Strict):
    label: str
    url: str

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("source url must be an https public link")
        return v


class Suggestion(Strict):
    label: str
    delete_order: bool = False
    replace_dose_mg: float | None = None


class CardText(Strict):
    indicator: Literal["info", "warning", "critical"] = "warning"
    summary: str
    detail: str
    why: list[str] = Field(min_length=1)
    suggestions: list[Suggestion] = Field(default_factory=list)
    override_reasons: list[tuple[str, str]] = Field(default_factory=list)


class Card(Strict):
    fire: CardText
    data_gap: CardText | None = None
    source: Source


class Predicate(Strict):
    """Declarative suppression predicate on the latest lab within a relative window."""

    lab: Literal["egfr", "creatinine", "potassium", "lithium", "weight"]
    op: Literal["<", "<=", ">", ">="]
    value: float
    within_days: int


class Match(Strict):
    # each group is a list of alternative drug classes; regimen needs a distinct drug per group
    groups: list[list[str]] = Field(min_length=1)


class Context(Strict):
    check: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    suppress_when_all: list[Predicate] = Field(default_factory=list)


class Rule(Strict):
    id: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str
    # What baseline mode actually checked (drug classes only). Titles describe the context condition
    # ("... with reduced eGFR"), which baseline never evaluates, so baseline cards use this instead.
    baseline_title: str | None = None
    status: Literal["prototype"]
    match: Match
    context: Context = Field(default_factory=Context)
    card: Card
