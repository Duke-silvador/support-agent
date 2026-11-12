from __future__ import annotations

import json
from typing import Any

from support_agent.providers.anthropic import AnthropicLLMProvider
from support_agent.providers.base import ChatMessage, ToolCall
from support_agent.providers.openai import OpenAILLMProvider

CALL = ToolCall(id="call_1", name="search_docs", arguments={"query": "refund window"})


def test_openai_assistant_echoes_tool_calls() -> None:
    message = ChatMessage(role="assistant", content="", tool_calls=(CALL,))
    payload: Any = OpenAILLMProvider._map(message)
    assert payload["role"] == "assistant"
    assert payload["content"] is None
    function = payload["tool_calls"][0]["function"]
    assert function["name"] == "search_docs"
    assert json.loads(function["arguments"]) == {"query": "refund window"}


def test_openai_tool_result_keeps_call_id() -> None:
    payload: Any = OpenAILLMProvider._map(
        ChatMessage(role="tool", content="passage", tool_call_id="call_1")
    )
    assert payload == {"role": "tool", "tool_call_id": "call_1", "content": "passage"}


def test_anthropic_groups_tool_results_after_tool_use() -> None:
    messages = [
        ChatMessage(role="user", content="refund?"),
        ChatMessage(role="assistant", content="", tool_calls=(CALL,)),
        ChatMessage(role="tool", content="passage one", tool_call_id="call_1"),
        ChatMessage(role="tool", content="passage two", tool_call_id="call_2"),
    ]
    built: list[Any] = AnthropicLLMProvider._build_messages(messages)
    assert [m["role"] for m in built] == ["user", "assistant", "user"]

    assistant_blocks = built[1]["content"]
    assert [block["type"] for block in assistant_blocks] == ["tool_use"]
    assert assistant_blocks[0]["id"] == "call_1"

    result_blocks = built[2]["content"]
    assert [block["type"] for block in result_blocks] == ["tool_result", "tool_result"]
    assert [block["tool_use_id"] for block in result_blocks] == ["call_1", "call_2"]


def test_anthropic_keeps_assistant_text_with_tool_use() -> None:
    messages = [
        ChatMessage(role="assistant", content="Looking that up.", tool_calls=(CALL,)),
        ChatMessage(role="tool", content="passage", tool_call_id="call_1"),
    ]
    built: list[Any] = AnthropicLLMProvider._build_messages(messages)
    assert [block["type"] for block in built[0]["content"]] == ["text", "tool_use"]


def test_system_messages_are_not_sent_as_chat_turns() -> None:
    built = AnthropicLLMProvider._build_messages(
        [ChatMessage(role="system", content="rules"), ChatMessage(role="user", content="hi")]
    )
    assert [m["role"] for m in built] == ["user"]
