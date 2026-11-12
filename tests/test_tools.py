from __future__ import annotations

from typing import Any

import pytest

from support_agent.agents import tools as tools_module
from support_agent.agents.tools import ToolContext, execute_tool
from support_agent.core.config import Settings


class FakeSession:
    """Only the bits the tool handlers touch."""

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = fail_commit

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_commit:
            raise TimeoutError

    async def rollback(self) -> None:
        self.rollbacks += 1


def _ctx(session: FakeSession) -> ToolContext:
    return ToolContext(
        session=session,  # type: ignore[arg-type]
        settings=Settings(_env_file=None),
        user_id="user-1",
        conversation_id="conv-1",
    )


_ESCALATION = {
    "reason": "billing",
    "summary": "Customer needs a human to look at the invoice.",
    "priority": "normal",
}


@pytest.mark.asyncio
async def test_escalation_is_never_requested_twice() -> None:
    session = FakeSession()
    ctx = _ctx(session)

    first = await execute_tool("escalate_to_human", _ESCALATION, ctx)
    assert first.ok is True
    assert first.approval is not None
    assert session.commits == 1

    # Even a second model turn cannot create a second pending approval.
    second = await execute_tool("escalate_to_human", _ESCALATION, ctx)
    assert second.ok is False
    assert "escalation_already_requested" in second.content
    assert session.commits == 1


@pytest.mark.asyncio
async def test_escalation_timeout_after_commit_blocks_a_retry_and_rolls_back() -> None:
    session = FakeSession(fail_commit=True)
    ctx = _ctx(session)

    outcome = await execute_tool("escalate_to_human", _ESCALATION, ctx)
    assert outcome.ok is False
    assert "timeout" in outcome.content
    # The session may be mid-flush; it must be rolled back so later calls work.
    assert session.rollbacks == 1

    # The attempt counts even though it failed: no duplicate approval.
    retry = await execute_tool("escalate_to_human", _ESCALATION, ctx)
    assert retry.ok is False
    assert "escalation_already_requested" in retry.content
    assert session.commits == 1


@pytest.mark.asyncio
async def test_search_releases_the_read_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_search(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(tools_module, "search_documents", fake_search)
    session = FakeSession()

    outcome = await execute_tool("search_docs", {"query": "refunds"}, _ctx(session))
    assert outcome.ok is True
    assert session.rollbacks == 1
