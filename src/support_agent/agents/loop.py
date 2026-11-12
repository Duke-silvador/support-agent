"""The agent loop.

Turn 1 forces ``search_docs`` so every answer is grounded in retrieval.
Tool results are appended to the transcript; the model then has to answer in
the JSON envelope. Citations are verified against the chunks that retrieval
actually returned (agents/verifier.py). Iterations are capped.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.prompts import build_system_message
from support_agent.agents.tools import (
    ApprovalRef,
    ToolContext,
    execute_tool,
    tool_specs,
)
from support_agent.agents.verifier import verify_citations
from support_agent.core.config import Settings
from support_agent.core.constants import REFUSAL_ANSWER, new_nonce
from support_agent.core.telemetry import current_trace_id, get_tracer
from support_agent.providers.base import ChatMessage
from support_agent.providers.factory import get_llm_provider
from support_agent.rag.retriever import RetrievedChunk

logger = logging.getLogger(__name__)
tracer = get_tracer()


@dataclass(slots=True)
class AgentAnswer:
    answer: str
    citations: list[RetrievedChunk] = field(default_factory=list)
    refused: bool = False
    used_tools: list[str] = field(default_factory=list)
    approval: ApprovalRef | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    iterations: int = 0


class _Envelope(BaseModel):
    answer: str = ""
    citations: list[Any] = Field(default_factory=list)

    @field_validator("citations", mode="before")
    @classmethod
    def _coerce_citations(cls, value: Any) -> list[Any]:
        # Real models sometimes emit null or a bare string; treat both sanely.
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return value
        return []


async def run_agent(
    session: AsyncSession,
    *,
    question: str,
    user_id: str,
    conversation_id: str,
    settings: Settings,
) -> AgentAnswer:
    llm = get_llm_provider()

    with tracer.start_as_current_span("chat.request") as span:
        # Pseudonymous: traces are exported off-host, so never put a raw user id
        # (often an email) in a span attribute. Logs keep it for audit.
        span.set_attribute("chat.user_hash", hashlib.sha256(user_id.encode()).hexdigest()[:16])
        span.set_attribute("chat.conversation_id", conversation_id)
        span.set_attribute("chat.question_chars", len(question))
        span.set_attribute("llm.provider", llm.name)
        span.set_attribute("llm.model", llm.model)

        trace_id = current_trace_id() or None
        nonce = new_nonce()
        ctx = ToolContext(
            session=session,
            settings=settings,
            user_id=user_id,
            conversation_id=conversation_id,
            trace_id=trace_id,
            nonce=nonce,
        )

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=build_system_message(nonce=nonce)),
            ChatMessage(role="user", content=question),
        ]
        allowed: dict[str, RetrievedChunk] = {}
        used_tools: list[str] = []
        approval: ApprovalRef | None = None
        search_done = False
        escalation_done = False
        tokens_in = tokens_out = 0
        text = ""
        finish_reason = "max_iterations"

        for iteration in range(settings.agent_max_iterations):
            permitted = _permitted_tools(
                search_done=search_done,
                escalation_done=escalation_done or ctx.escalation_attempted,
            )
            force_tool = "search_docs" if iteration == 0 else None
            with tracer.start_as_current_span("llm.completion") as llm_span:
                llm_span.set_attribute("llm.iteration", iteration)
                llm_span.set_attribute("llm.tools", ",".join(sorted(permitted)))
                result = await llm.complete(
                    messages,
                    tools=tool_specs(allow=permitted),
                    force_tool=force_tool,
                    temperature=0.0,
                    max_tokens=settings.max_output_tokens,
                )
                llm_span.set_attribute("llm.tokens.input", result.input_tokens)
                llm_span.set_attribute("llm.tokens.output", result.output_tokens)
                llm_span.set_attribute("llm.finish_reason", result.finish_reason)
            tokens_in += result.input_tokens
            tokens_out += result.output_tokens

            if not result.tool_calls:
                text = result.text
                finish_reason = result.finish_reason
                break

            messages.append(
                ChatMessage(
                    role="assistant",
                    content=result.text or "",
                    tool_calls=tuple(result.tool_calls),
                )
            )
            for call in result.tool_calls:
                # Recompute on every call: a model may emit several tool calls in
                # one turn (OpenAI parallel calls). This enforces "at most one
                # search and one escalation" per chat.
                permitted = _permitted_tools(
                    search_done=search_done,
                    escalation_done=escalation_done or ctx.escalation_attempted,
                )
                if call.name not in permitted:
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=json.dumps({"error": "tool_not_allowed", "tool": call.name}),
                            tool_call_id=call.id,
                        )
                    )
                    continue
                outcome = await execute_tool(call.name, call.arguments, ctx)
                messages.append(
                    ChatMessage(role="tool", content=outcome.content, tool_call_id=call.id)
                )
                if not outcome.ok:
                    # Do not advance the state machine on failure: the model may
                    # retry the same tool within the iteration cap.
                    continue
                used_tools.append(call.name)
                if call.name == "search_docs":
                    search_done = True
                    allowed.update(outcome.chunks)
                elif call.name == "escalate_to_human":
                    # An attempt counts even if it timed out after committing.
                    escalation_done = escalation_done or ctx.escalation_attempted
                    if outcome.approval is not None:
                        approval = outcome.approval

        if not text.strip():
            # Loop exhausted or model returned nothing usable.
            text = REFUSAL_ANSWER
            logger.warning(
                "agent produced no final answer",
                extra={"iterations": settings.agent_max_iterations, "finish": finish_reason},
            )

        raw_answer, claimed = parse_envelope(text)
        with tracer.start_as_current_span("guardrail.citations") as guard_span:
            guard_span.set_attribute("guardrail.claimed_citations", len(claimed))
            verification = verify_citations(raw_answer, claimed, allowed)
            guard_span.set_attribute("guardrail.citations", len(verification.citations))
            guard_span.set_attribute("guardrail.dropped_citations", len(verification.dropped_ids))
            guard_span.set_attribute("guardrail.refused", verification.refused)

        span.set_attribute("chat.refused", verification.refused)
        span.set_attribute("chat.citations", len(verification.citations))
        span.set_attribute("chat.tools", ",".join(used_tools))
        span.set_attribute("llm.tokens.input", tokens_in)
        span.set_attribute("llm.tokens.output", tokens_out)
        span.set_attribute("chat.iterations", iteration + 1)
        if approval is not None:
            span.set_attribute("approval.id", str(approval.id))
        if verification.dropped_ids:
            span.set_attribute("chat.dropped_citations", len(verification.dropped_ids))

        return AgentAnswer(
            answer=verification.answer,
            citations=verification.citations,
            refused=verification.refused,
            used_tools=used_tools,
            approval=approval,
            model=llm.model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            iterations=iteration + 1,
        )


def _permitted_tools(*, search_done: bool, escalation_done: bool) -> frozenset[str]:
    """Strict progression: search first, then at most one escalation, then answer.

    The model never sees a tool it is not allowed to use, and after the search
    it cannot search again (no unbounded retrieval loops / cost blow-ups).
    """
    if not search_done:
        return frozenset({"search_docs"})
    if not escalation_done:
        return frozenset({"escalate_to_human"})
    return frozenset()


def parse_envelope(text: str) -> tuple[str, list[str]]:
    """Extract ``{"answer", "citations"}``; degrade gracefully on free text."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            envelope = _Envelope.model_validate(payload)
        except ValidationError:
            continue
        if not envelope.answer.strip():
            continue
        return envelope.answer.strip(), _citation_ids(envelope.citations)

    return raw, []


def _citation_ids(items: list[Any]) -> list[str]:
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            value = item.get("chunk_id") or item.get("id")
            if isinstance(value, str):
                ids.append(value)
    return ids
