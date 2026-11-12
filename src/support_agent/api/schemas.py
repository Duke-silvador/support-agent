"""API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from support_agent.core.config import get_settings

_SETTINGS = get_settings()
_MAX_QUESTION = _SETTINGS.max_question_chars


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    checks: dict[str, str] = Field(default_factory=dict)
    providers: dict[str, str] = Field(default_factory=dict)


class DocumentResponse(BaseModel):
    document_id: UUID
    filename: str
    title: str
    chunk_count: int
    duplicate: bool
    injection_flags: list[str]
    created_at: datetime
    owner_id: str
    visibility: Literal["private", "public"]


class ChatRequest(BaseModel):
    question: str = Field(
        min_length=3,
        max_length=_MAX_QUESTION,
        description="Customer question, in any language.",
    )
    conversation_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$",
        description="Optional client-side conversation id for audit/tracing.",
    )


class CitationSchema(BaseModel):
    chunk_id: str
    source: str = Field(description="Source document filename")
    section: str = Field(description="Markdown heading, or 'document'")
    score: float = Field(ge=0.0, le=1.0)
    snippet: str = Field(description="Short excerpt of the cited passage")


class UsageSchema(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ApprovalRefSchema(BaseModel):
    id: UUID
    status: Literal["pending", "approved", "rejected"]


class ChatResponse(BaseModel):
    answer: str
    citations: list[CitationSchema]
    refused: bool
    used_tools: list[str]
    approval: ApprovalRefSchema | None = None
    conversation_id: str
    model: str
    usage: UsageSchema
    trace_id: str
    cached: bool = False


class ApprovalDecision(BaseModel):
    # Loudly reject legacy/forged fields such as `decided_by`: identity comes
    # from the authenticated caller only.
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)


class ApprovalResponse(BaseModel):
    id: UUID
    status: Literal["pending", "approved", "rejected"]
    user_id: str
    conversation_id: str
    reason: str
    summary: str
    priority: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    ticket_ref: str | None = None
    dispatch_failed: bool = False
