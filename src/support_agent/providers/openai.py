"""OpenAI chat + embeddings. Both protocol implementations live here."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from support_agent.core.config import Settings
from support_agent.providers.base import (
    ChatMessage,
    EmbeddingProvider,
    LLMProvider,
    LLMResult,
    ToolCall,
    ToolSpec,
)

logger = logging.getLogger(__name__)

_EMBED_BATCH = 96


def _client(settings: Settings) -> AsyncOpenAI:
    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    return AsyncOpenAI(
        api_key=api_key,
        timeout=settings.provider_timeout_seconds,
        # Retries are owned by our tool runner so we can trace each attempt.
        max_retries=0,
    )


class OpenAILLMProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.llm_model
        self._client = _client(settings)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        force_tool: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._map(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        if force_tool is not None:
            payload["tool_choice"] = {"type": "function", "function": {"name": force_tool}}

        completion = await self._client.chat.completions.create(**payload)
        choice = completion.choices[0]
        message = choice.message
        calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            if call.type != "function":
                continue
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                logger.warning(
                    "discarding malformed tool arguments", extra={"tool": call.function.name}
                )
                arguments = {}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

        usage = completion.usage
        return LLMResult(
            text=message.content or "",
            model=completion.model,
            tool_calls=calls,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            finish_reason=choice.finish_reason or "stop",
        )

    @staticmethod
    def _map(message: ChatMessage) -> ChatCompletionMessageParam:
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.content,
            }
        if message.role == "assistant":
            if message.tool_calls:
                return {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            return {"role": "assistant", "content": message.content}
        if message.role == "system":
            return {"role": "system", "content": message.content}
        return {"role": "user", "content": message.content}


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.embedding_model
        self.dim = settings.embedding_dim
        self._client = _client(settings)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = list(texts[start : start + _EMBED_BATCH])
            response = await self._client.embeddings.create(
                model=self.model, input=batch, timeout=self.settings.provider_timeout_seconds
            )
            for item in sorted(response.data, key=lambda d: d.index):
                vector = list(item.embedding)
                if len(vector) != self.dim:
                    raise RuntimeError(
                        f"embedding dim mismatch: model returned {len(vector)}, "
                        f"EMBEDDING_DIM={self.dim}"
                    )
                vectors.append(vector)
        return vectors
