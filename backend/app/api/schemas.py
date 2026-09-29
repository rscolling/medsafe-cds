"""Pydantic models for CDS Hooks requests/responses (CDS Hooks 2.0) and the UI helper API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CdsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    hook: str
    hookInstance: str = Field(min_length=8, max_length=128)
    fhirServer: str | None = None
    context: dict[str, Any]
    prefetch: dict[str, Any] | None = None
    extension: dict[str, Any] | None = None


class Coding(BaseModel):
    code: str
    system: str
    display: str | None = None


class OverrideReason(BaseModel):
    reason: Coding | None = None
    userComment: str | None = Field(default=None, max_length=2000)


class AcceptedSuggestion(BaseModel):
    id: str


class FeedbackItem(BaseModel):
    card: str
    outcome: Literal["accepted", "overridden"]
    acceptedSuggestions: list[AcceptedSuggestion] | None = None
    overrideReason: OverrideReason | None = None
    outcomeTimestamp: str


class FeedbackRequest(BaseModel):
    feedback: list[FeedbackItem] = Field(min_length=1, max_length=100)


class CompareRequest(BaseModel):
    source: Literal["fhir", "vista"]
    patientId: str
    rxcui: str
