"""ASGI request-body guards.

Starlette parses multipart bodies *before* the route runs and spools file parts
to a temporary file, so a ``Transfer-Encoding: chunked`` upload with no
``Content-Length`` can fill the disk before ``POST /documents`` sees it. JSON
endpoints have the same problem in memory: FastAPI reads the whole body before
dependencies (including auth and rate limiting) run.

This middleware caps the actual bytes received, so it is chunked-safe, and
rejects before the app buffers the body. Requests that declare an acceptable
``Content-Length`` are passed straight through without buffering.
"""

from __future__ import annotations

from typing import cast

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_UPLOAD_PATH = "/documents"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, upload_max_bytes: int, json_max_bytes: int) -> None:
        self.app = app
        self.upload_max_bytes = upload_max_bytes
        self.json_max_bytes = json_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = dict(cast("list[tuple[bytes, bytes]]", scope.get("headers", [])))
        declared = headers.get(b"content-length", b"")
        if declared.isdigit():
            if int(declared) > limit:
                await self._reject(scope, receive, send)
                return
            # Known size and within the cap: no need to buffer or replay.
            await self.app(scope, receive, send)
            return

        buffered: list[bytes] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                await self._reject(scope, receive, send)
                return
            buffered.append(chunk)
            more = message.get("more_body", False)

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(buffered):
                body = buffered[index]
                index += 1
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": index < len(buffered),
                }
            # The body is exhausted. Do not fake a disconnect: Starlette may call
            # receive() again to check for client disconnect.
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay, send)

    def _limit_for(self, scope: Scope) -> int | None:
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            return None
        if scope.get("path") == _UPLOAD_PATH:
            return self.upload_max_bytes
        # Every other request that may carry a body is capped at the JSON limit.
        # Content-Type is attacker-controlled, so it must not decide whether a
        # cap applies: an uncapped `text/plain` POST would be read into memory
        # by FastAPI before auth and the rate limiter run.
        return self.json_max_bytes

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(status_code=413, content={"detail": "request body too large"})
        await response(scope, receive, send)
