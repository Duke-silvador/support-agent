from __future__ import annotations

from typing import Any

import pytest

from support_agent.api.middleware import BodySizeLimitMiddleware

_SCOPE: dict[str, Any] = {
    "type": "http",
    "method": "POST",
    "path": "/documents",
    "query_string": b"",
    "headers": [],
}


def _json_scope(path: str = "/chat") -> dict[str, Any]:
    return {
        **_SCOPE,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
    }


async def _drain(app_seen: dict[str, Any], scope: Any, receive: Any, send: Any) -> None:
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        app_seen["body"] = app_seen.get("body", b"") + message.get("body", b"")
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.asyncio
async def test_declared_content_length_too_large_is_rejected() -> None:
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10, json_max_bytes=10)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:  # pragma: no cover - must not be read
        raise AssertionError("body should not be read")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware({**_SCOPE, "headers": [(b"content-length", b"999")]}, receive, send)
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_body_over_limit_is_rejected() -> None:
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10, json_max_bytes=10)
    chunks = [
        {"type": "http.request", "body": b"a" * 6, "more_body": True},
        {"type": "http.request", "body": b"b" * 6, "more_body": False},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return chunks.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(_SCOPE, receive, send)
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_json_over_limit_is_rejected() -> None:
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10_000, json_max_bytes=10)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{'q':" + b"x" * 50, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(_json_scope(), receive, send)
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_declared_json_length_over_cap_is_rejected() -> None:
    called = False

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called = True

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10_000, json_max_bytes=10)
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("body should not be read")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        **_json_scope(),
        "headers": [(b"content-type", b"application/json"), (b"content-length", b"11")],
    }
    await middleware(scope, receive, send)
    assert called is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_body_under_limit_is_replayed_and_terminates_cleanly() -> None:
    seen: dict[str, Any] = {}
    terminal: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _drain(seen, scope, receive, send)
        terminal.append(await receive())

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10, json_max_bytes=10)
    chunks = [
        {"type": "http.request", "body": b"1234", "more_body": True},
        {"type": "http.request", "body": b"5678", "more_body": False},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return chunks.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(_SCOPE, receive, send)
    assert seen["body"] == b"12345678"
    assert terminal[0] == {"type": "http.request", "body": b"", "more_body": False}
    assert sent[0]["status"] == 204


@pytest.mark.asyncio
async def test_declared_length_under_cap_is_passed_through_without_buffering() -> None:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["called"] = True
        await _drain(seen, scope, receive, send)

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10_000, json_max_bytes=10_000)
    chunks = [{"type": "http.request", "body": b"1234", "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return chunks.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {**_SCOPE, "headers": [(b"content-length", b"4")]}
    await middleware(scope, receive, send)
    assert seen["called"] is True
    assert seen["body"] == b"1234"


@pytest.mark.asyncio
async def test_non_json_bodies_are_capped_by_content_not_content_type() -> None:
    called = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal called
        called += 1

    middleware = BodySizeLimitMiddleware(app, upload_max_bytes=10, json_max_bytes=10)

    async def too_big() -> dict[str, Any]:
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        return None

    # Methods without a body are not buffered or capped.
    await middleware({**_SCOPE, "method": "GET", "path": "/health"}, too_big, send)
    await middleware({**_json_scope(), "method": "GET"}, too_big, send)
    assert called == 2

    # Content-Type is attacker-controlled: text/plain must not bypass the cap.
    sent: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        sent.append(message)

    text_scope = {**_SCOPE, "path": "/chat", "headers": [(b"content-type", b"text/plain")]}
    await middleware(text_scope, too_big, capture)
    assert called == 2, "the app must not see an uncapped body"
    assert sent[0]["status"] == 413
