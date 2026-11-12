"""Test bootstrap: force the deterministic stack before anything imports config."""

from __future__ import annotations

import os

os.environ["ENVIRONMENT"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["EMBEDDING_DIM"] = "1536"  # must match the migrated column
os.environ["OTEL_ENABLED"] = "false"
os.environ["RATE_LIMIT_REQUESTS"] = "20"
os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
# Explicit operator allowlist: empty now denies decisions by default.
os.environ["OPERATOR_IDS"] = "operator-1,operator-2,operator-9"
os.environ["ALLOW_ANON_OPERATORS"] = "false"
# Tests exercise the deterministic local identity stub. OIDC is covered by unit
# tests that construct a verifier directly, so no JWKS endpoint is needed here.
os.environ["AUTH_MODE"] = "header"
# Keep retries fast in tests (the integration suite drives the worker inline).
os.environ["OUTBOX_POLL_SECONDS"] = "0.05"
os.environ["OUTBOX_BASE_BACKOFF_SECONDS"] = "0.05"
os.environ["OUTBOX_STALE_SECONDS"] = "30"

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration tests unless explicitly requested.

    Two ways to opt in: ``-m integration`` / ``-m "integration or ..."``, or
    ``RUN_INTEGRATION=1`` (used by the compose test service and CI).
    """
    marker_expr = config.getoption("-m", default="") or ""
    if "integration" in marker_expr or os.environ.get("RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="integration test: run with -m integration or RUN_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
