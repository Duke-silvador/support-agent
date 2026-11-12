"""Provider contracts.

Two independent seams so a deployment can mix vendors:

    LLMProvider        chat/completions + optional tool calling
    EmbeddingProvider   vectors

Anthropic implements chat only, so ``EMBEDDING_PROVIDER=anthropic`` does not
exist by design (OpenAI or ``fake`` for embeddings).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str
    tool_call_id: str | None = None
    # Present on assistant turns that requested tools. Real providers must echo
    # the ids back when they send tool results, so the transcript keeps them.
    tool_calls: tuple[ToolCall, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            payload["tool_calls"] = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in self.tool_calls
            ]
        return payload


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        force_tool: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> LLMResult: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
