"""Tiny behavioural eval runner (no vendor SDKs, no notebook).

Usage:
    docker compose up -d
    python -m evals.runner

Environment:
    EVAL_BASE_URL   default http://localhost:8000
    EVAL_USER_ID    default random per run (avoids the 20/min rate limit)

Exit code 0 = all cases passed, 1 = at least one failed. Designed for CI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import yaml

DATA_DIR = Path(__file__).resolve().parent / "data"
CASES_FILE = Path(__file__).resolve().parent / "cases.yaml"
BASE_URL = os.environ.get("EVAL_BASE_URL", "http://localhost:8000")
RUN_ID = uuid4().hex[:8]

_ALLOWED_EXPECT = {
    "answered",
    "citations_min",
    "citations_from",
    "must_include",
    "must_not_include",
    "approval",
    "used_tool",
}


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class CaseResult:
    case_id: str
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def load_cases() -> list[dict[str, Any]]:
    payload = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not cases:
        raise SystemExit(f"no cases found in {CASES_FILE}")
    for case in cases:
        expect = case.get("expect")
        if not isinstance(case.get("id"), str) or not isinstance(case.get("question"), str):
            raise SystemExit(f"case needs id+question: {case!r}")
        if not isinstance(expect, dict) or not expect:
            raise SystemExit(f"case {case['id']}: expect must be a non-empty mapping")
        # A typo in an expectation key must fail loudly instead of silently
        # producing zero checks (which used to count as a pass).
        unknown = set(expect) - _ALLOWED_EXPECT
        if unknown:
            raise SystemExit(f"case {case['id']}: unknown expectation keys {sorted(unknown)}")
        _validate_expect(str(case["id"]), expect)
    return cases


def _validate_expect(case_id: str, expect: dict[str, Any]) -> None:
    """Reject wrong value types: a malformed expectation must never pass."""

    def fail(message: str) -> None:
        raise SystemExit(f"case {case_id}: {message}")

    if "answered" in expect and not isinstance(expect["answered"], bool):
        fail("answered must be a boolean")
    if "citations_min" in expect:
        value = expect["citations_min"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            fail("citations_min must be a non-negative integer")
    for key in ("citations_from", "must_include", "must_not_include"):
        if key not in expect:
            continue
        value = expect[key]
        if not isinstance(value, list) or not value:
            fail(f"{key} must be a non-empty list")
        if not all(isinstance(item, str) and item.strip() for item in value):
            fail(f"{key} must contain non-empty strings")
    if "approval" in expect and expect["approval"] not in {"pending", "approved", "rejected"}:
        fail("approval must be pending, approved or rejected")
    if "used_tool" in expect and not (
        isinstance(expect["used_tool"], str) and expect["used_tool"].strip()
    ):
        fail("used_tool must be a non-empty string")


async def ensure_documents(client: httpx.AsyncClient, user: str) -> None:
    headers = {"X-User-ID": user}
    for path in sorted(DATA_DIR.glob("*.md")):
        response = await client.post(
            "/documents",
            files={"file": (path.name, path.read_bytes(), "text/markdown")},
            headers=headers,
        )
        if response.status_code not in (200, 201):
            raise SystemExit(
                f"failed to ingest {path.name}: {response.status_code} {response.text}"
            )
        body = response.json()
        flags = ",".join(body.get("injection_flags") or [])
        print(
            f"  ingested {path.name:<24} chunks={body['chunk_count']:<2} "
            f"duplicate={str(body['duplicate']).lower():<5} flags=[{flags}]"
        )


def evaluate(case: dict[str, Any], body: dict[str, Any]) -> CaseResult:
    expect = case.get("expect") or {}
    citations = body.get("citations") or []
    answer = str(body.get("answer") or "")
    lowered = answer.lower()
    checks: list[Check] = [
        # A cache hit would prove nothing about the pipeline.
        Check("not_cached", body.get("cached") is False, f"cached={body.get('cached')}")
    ]

    if "answered" in expect:
        want_answered = bool(expect["answered"])
        got_answered = not body.get("refused", False)
        checks.append(
            Check(
                "answered",
                got_answered is want_answered,
                f"expected answered={want_answered}, got refused={body.get('refused')}",
            )
        )

    if "citations_min" in expect:
        minimum = int(expect["citations_min"])
        checks.append(
            Check("citations_min", len(citations) >= minimum, f"{len(citations)} >= {minimum}")
        )

    if "citations_from" in expect:
        sources = {str(c.get("source")) for c in citations}
        wanted = {str(item) for item in expect["citations_from"]}
        # All-of: a pass proves every listed eval fixture was actually cited,
        # so ranking cannot be satisfied by an unrelated eval document.
        checks.append(
            Check(
                "citations_from",
                wanted.issubset(sources),
                f"cited={sorted(sources)} missing={sorted(wanted - sources)}",
            )
        )

    for needle in expect.get("must_include") or []:
        checks.append(Check(f"includes:{needle}", str(needle).lower() in lowered, answer[:160]))

    for needle in expect.get("must_not_include") or []:
        checks.append(Check(f"excludes:{needle}", str(needle).lower() not in lowered, answer[:160]))

    if "approval" in expect:
        approval = body.get("approval") or {}
        checks.append(
            Check(
                "approval",
                approval.get("status") == expect["approval"],
                f"status={approval.get('status')}",
            )
        )

    if "used_tool" in expect:
        used = body.get("used_tools") or []
        checks.append(Check("used_tool", expect["used_tool"] in used, f"used={used}"))

    return CaseResult(case_id=str(case["id"]), checks=checks)


async def run() -> int:
    cases = load_cases()
    user = os.environ.get("EVAL_USER_ID") or f"eval-{RUN_ID}"
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=90.0) as client:
        try:
            health = await client.get("/health")
        except httpx.HTTPError as exc:
            print(f"cannot reach {BASE_URL}: {exc}")
            return 2
        providers = health.json().get("providers") or {}
        print(f"Evals: {len(cases)} cases -> {BASE_URL}")
        print(
            f"Providers: llm={providers.get('llm', '?')} "
            f"embedding={providers.get('embedding', '?')} "
            f"model={providers.get('llm_model', '?')}"
        )
        if providers.get("llm") == "fake":
            print(
                "NOTE: fake providers are deterministic plumbing. These results validate the "
                "pipeline contract (retrieval -> prompt -> citations -> refusal -> approval), "
                "NOT retrieval or answer quality. Use a real model for quality numbers.\n"
            )

        print("Documents:")
        # Ingest as the same identity that asks the questions. Documents are
        # private by default and retrieval is owner-scoped (public OR owner), so
        # a separate "-ingest" user would make every in-scope question refuse.
        await ensure_documents(client, user)
        print("\nCases:")

        results: list[CaseResult] = []
        for case in cases:
            response = await client.post(
                "/chat",
                json={
                    "question": case["question"],
                    "conversation_id": f"eval-{RUN_ID}-{case['id']}"[:128],
                },
                # Force a fresh answer: reruns inside the cache TTL must still
                # exercise retrieval and the model.
                headers={"X-User-ID": user, "Cache-Control": "no-cache"},
            )
            if response.status_code != 200:
                results.append(
                    CaseResult(
                        case_id=str(case["id"]),
                        checks=[
                            Check("http", False, f"{response.status_code} {response.text[:200]}")
                        ],
                    )
                )
                print(f"[FAIL] {case['id']}: HTTP {response.status_code}")
                continue

            result = evaluate(case, response.json())
            results.append(result)
            status = "PASS" if result.ok else "FAIL"
            print(f"[{status}] {case['id']:<28} checks={len(result.checks)}")
            for failure in result.failures():
                print(f"         - {failure.name}: {failure.detail}")

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    if total != len(cases):  # pragma: no cover - defensive
        print(f"expected {len(cases)} results, got {total}")
        return 1
    print(f"\n{passed}/{total} cases passed")
    return 0 if passed == total else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
