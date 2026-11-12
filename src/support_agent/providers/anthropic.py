"""Anthropic chat provider.

Anthropic does not serve embeddings, so this module intentionally implements
``LLMProvider`` only. Pair it with ``EMBEDDING_PROVIDER=openai`` (or ``fake``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolParam

from support_agent.core.config import Settings
from support_agent.providers.base import ChatMessage, LLMProvider, LLMResult, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class AnthropicLLMProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.llm_model
        api_key = (
            settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
        )
        self._client = AsyncAnthropic(
            api_key=api_key,
            timeout=settings.provider_timeout_seconds,
            max_retries=0,
        )

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        force_tool: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> LLMResult:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self._build_messages(messages),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                ToolParam(name=t.name, description=t.description, input_schema=t.parameters)
                for t in tools
            ]
        if force_tool is not None:
            payload["tool_choice"] = {"type": "tool", "name": force_tool}

        response = await self._client.messages.create(**payload)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                if not arguments:
                    logger.warning("empty tool input", extra={"tool": block.name})
                calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

        return LLMResult(
            text="".join(text_parts),
            model=response.model,
            tool_calls=calls,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            finish_reason=response.stop_reason or "stop",
        )

    @staticmethod
    def _build_messages(messages: Sequence[ChatMessage]) -> list[MessageParam]:
        """Translate the internal transcript into Anthropic content blocks.

        Anthropic pairs each ``tool_use`` block with a ``tool_result`` block in
        the *next user message*, so consecutive tool results are grouped.
        """
        built: list[MessageParam] = []
        pending: list[dict[str, Any]] = []

        def flush_tools() -> None:
            if pending:
                built.append(cast(MessageParam, {"role": "user", "content": list(pending)}))
                pending.clear()

        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                pending.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": message.content,
                    }
                )
                continue
            flush_tools()
            if message.role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
                if blocks:
                    built.append(cast(MessageParam, {"role": "assistant", "content": blocks}))
                continue
            built.append(cast(MessageParam, {"role": "user", "content": message.content}))
        flush_tools()
        return built
