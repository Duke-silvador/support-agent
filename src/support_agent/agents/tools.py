"""Agent tools.

Only two tools exist, both least-privilege:

* ``search_docs``        read-only SELECT over pre-indexed chunks (fixed query).
* ``escalate_to_human``  creates a *pending approval*; it performs no external
                          side effect. A human decides via POST /approvals/{id}.

Every call is validated (Pydantic), time-boxed, retried at most once, traced, and
its error text is sanitised before it reaches the model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.prompts import render_context
from support_agent.core.config import Settings
from support_agent.core.telemetry import get_tracer
from support_agent.db.models import Approval
from support_agent.providers.base import ToolSpec
from support_agent.providers.factory import get_embedding_provider
from support_agent.rag.retriever import RetrievedChunk, search_documents

logger = logging.getLogger(__name__)
tracer = get_tracer()


@dataclass(slots=True)
class ToolContext:
    session: AsyncSession
    settings: Settings
    user_id: str
    conversation_id: str
    trace_id: str | None = None
    nonce: str = ""
    # Set *before* the escalation handler runs so a timeout after its commit can
    # never produce a second pending approval for the same conversation.
    escalation_attempted: bool = False


@dataclass(slots=True)
class ApprovalRef:
    id: UUID
    status: str
    reason: str


@dataclass(slots=True)
class ToolOutcome:
    content: str
    ok: bool
    chunks: dict[str, RetrievedChunk] = field(default_factory=dict)
    approval: ApprovalRef | None = None


class SearchDocsArgs(BaseModel):
    query: str = Field(
        min_length=2,
        max_length=300,
        description="Short search query, in the user's language, 2-300 characters.",
    )


class EscalateArgs(BaseModel):
    reason: str = Field(
        min_length=3,
        max_length=500,
        description="Why a human is needed (category or short reason).",
    )
    summary: str = Field(
        min_length=10,
        max_length=1000,
        description="What the human needs to know, without guessing at facts.",
    )
    priority: str = Field(
        default="normal",
        pattern="^(low|normal|high|urgent)$",
        description="Ticket priority: low, normal, high, urgent.",
    )


async def _search_docs(args: BaseModel, ctx: ToolContext) -> ToolOutcome:
    assert isinstance(args, SearchDocsArgs)
    results = await search_documents(
        ctx.session,
        query=args.query,
        embedder=get_embedding_provider(),
        top_k=ctx.settings.retrieval_top_k,
        min_score=ctx.settings.retrieval_min_score,
        viewer_id=ctx.user_id,
    )
    # Release the read transaction before the model round-trip. Without this the
    # connection stays checked out for the whole LLM call (pool exhaustion).
    await ctx.session.rollback()
    if not results:
        return ToolOutcome(
            content="No matching passages found in the company documents.",
            ok=True,
        )
    payload = render_context(results, nonce=ctx.nonce or None)
    return ToolOutcome(
        content=payload,
        ok=True,
        chunks={r.citation_id: r for r in results},
    )


async def _escalate_to_human(args: BaseModel, ctx: ToolContext) -> ToolOutcome:
    assert isinstance(args, EscalateArgs)
    approval = Approval(
        status="pending",
        user_id=ctx.user_id,
        conversation_id=ctx.conversation_id,
        reason=args.reason[:500],
        summary=args.summary,
        payload={"priority": args.priority, "requested_via": "chat"},
        trace_id=ctx.trace_id,
    )
    ctx.session.add(approval)
    await ctx.session.commit()
    # No refresh: `id` is assigned client-side (uuid4) and `expire_on_commit`
    # is off, so refreshing would only open a new transaction before the LLM
    # round-trip.
    logger.info(
        "escalation requested (pending human approval)",
        extra={"approval_id": str(approval.id), "user_id": ctx.user_id},
    )
    return ToolOutcome(
        content=json.dumps(
            {
                "approval_id": str(approval.id),
                "status": "pending_human_approval",
                "message": (
                    "A human must approve this escalation before any ticket is created. "
                    "Tell the user it is pending."
                ),
            }
        ),
        ok=True,
        approval=ApprovalRef(id=approval.id, status="pending", reason=args.reason[:500]),
    )


@dataclass(frozen=True, slots=True)
class ToolDef:
    spec: ToolSpec
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[ToolOutcome]]


_TOOLS: dict[str, ToolDef] = {
    "search_docs": ToolDef(
        spec=ToolSpec(
            name="search_docs",
            description=(
                "Search the company knowledge base (read-only). Returns the most "
                "relevant passages, each wrapped in UNTRUSTED_DOC markers with a "
                "chunk id you can cite."
            ),
            parameters=SearchDocsArgs.model_json_schema(),
        ),
        args_model=SearchDocsArgs,
        handler=_search_docs,
    ),
    "escalate_to_human": ToolDef(
        spec=ToolSpec(
            name="escalate_to_human",
            description=(
                "File a request for a human colleague. This only creates a PENDING "
                "approval; a human must approve it before a ticket exists. Never "
                "promise the user that a human has already responded."
            ),
            parameters=EscalateArgs.model_json_schema(),
        ),
        args_model=EscalateArgs,
        handler=_escalate_to_human,
    ),
}

TOOL_NAMES = frozenset(_TOOLS)


def tool_specs(*, allow: frozenset[str] | None = None) -> list[ToolSpec]:
    names = allow if allow is not None else TOOL_NAMES
    return [_TOOLS[name].spec for name in sorted(names) if name in _TOOLS]


async def execute_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> ToolOutcome:
    with tracer.start_as_current_span(f"tool.{name}") as span:
        span.set_attribute("tool.name", name)
        definition = _TOOLS.get(name)
        if definition is None:
            span.set_attribute("tool.ok", False)
            # Hallucinated tool name: refuse, don't invent behaviour.
            return ToolOutcome(
                content=json.dumps({"error": "unknown_tool", "allowed": sorted(TOOL_NAMES)}),
                ok=False,
            )

        try:
            parsed = definition.args_model.model_validate(arguments)
        except ValidationError as exc:
            span.set_attribute("tool.ok", False)
            return ToolOutcome(
                content=json.dumps(
                    {"error": "invalid_arguments", "details": exc.errors(include_url=False)[:3]}
                ),
                ok=False,
            )

        # Escalations are not retried: the handler commits an approval row, so a
        # timeout after commit could otherwise create a duplicate ticket request.
        # The flag is set before the handler runs and is authoritative across the
        # whole agent loop, not just this call.
        if name == "escalate_to_human":
            if ctx.escalation_attempted:
                span.set_attribute("tool.ok", False)
                return ToolOutcome(
                    content=json.dumps(
                        {
                            "error": "escalation_already_requested",
                            "message": "An escalation was already requested in this "
                            "conversation. Do not request another one.",
                        }
                    ),
                    ok=False,
                )
            ctx.escalation_attempted = True

        attempts = 1 if name == "escalate_to_human" else ctx.settings.tool_max_retries + 1
        last_error = "internal_error"
        for attempt in range(1, attempts + 1):
            try:
                async with asyncio.timeout(ctx.settings.tool_timeout_seconds):
                    outcome = await definition.handler(parsed, ctx)
                span.set_attribute("tool.ok", outcome.ok)
                span.set_attribute("tool.attempt", attempt)
                if outcome.approval is not None:
                    span.set_attribute("approval.id", str(outcome.approval.id))
                return outcome
            except TimeoutError:
                last_error = "timeout"
                logger.warning("tool timeout", extra={"tool": name, "attempt": attempt})
                # The session may be mid-flush (e.g. a timed-out commit); a later
                # tool call would otherwise raise PendingRollbackError.
                await ctx.session.rollback()
            except Exception:
                last_error = "internal_error"
                logger.exception("tool failed", extra={"tool": name, "attempt": attempt})
                await ctx.session.rollback()

        span.set_attribute("tool.ok", False)
        span.set_attribute("tool.attempts", attempts)
        return ToolOutcome(
            content=json.dumps(
                {
                    "error": last_error,
                    "message": "The tool failed. Do not invent an answer; tell the user "
                    "you could not retrieve the information.",
                }
            ),
            ok=False,
        )
