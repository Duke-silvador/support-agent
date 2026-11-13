"""Behavioural verification of the OIDC path against a live Keycloak.

This is not a unit test: it needs `keycloak` + `postgres` + `redis` and the API
running with ``AUTH_MODE=oidc``. It prints PASS/FAIL per check and exits 1 if
any check fails, so it can be used as a release gate.

Run it **inside the compose network** (tokens must be minted from the same
network so the ``iss`` claim matches ``OIDC_ISSUER``)::

    docker compose --profile auth up -d --wait keycloak
    docker compose run -d --name oidc-app --no-deps --service-ports \\
        -e AUTH_MODE=oidc -e OTEL_ENABLED=false -e OPERATOR_IDS= app
    docker compose --profile test run --rm --no-deps \\
        -v ./scripts:/scripts:ro \\
        -e OIDC_VERIFY_BASE_URL=http://oidc-app:8000 \\
        test python /scripts/verify_oidc.py

``OPERATOR_IDS=`` is intentional: with an empty allowlist the only way to be an
operator is the IdP role claim (``OIDC_OPERATOR_ROLE``), so the role path is
actually exercised instead of the allowlist shortcut.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import uuid

import httpx

BASE_URL = os.environ.get("OIDC_VERIFY_BASE_URL", "http://app:8000")
TOKEN_URL = os.environ.get(
    "OIDC_VERIFY_TOKEN_URL",
    "http://keycloak:8080/realms/support-agent/protocol/openid-connect/token",
)
CLIENT_ID = os.environ.get("OIDC_VERIFY_CLIENT_ID", "support-agent-api")
ISSUER = os.environ.get("OIDC_VERIFY_ISSUER", "http://keycloak:8080/realms/support-agent")
AUDIENCE = os.environ.get("OIDC_VERIFY_AUDIENCE", "support-agent-api")
RATE_LIMIT = int(os.environ.get("OIDC_VERIFY_RATE_LIMIT", "20"))

PASSWORDS = {
    "agent-user": "agent-user-local-pw",
    "other-user": "other-user-local-pw",
    "operator-1": "operator-1-local-pw",
}

RUN = uuid.uuid4().hex[:8]
RESULTS: list[tuple[str, bool]] = []


def _unsigned_token() -> str:
    """Forge an ``alg=none`` token at runtime (no literal JWT in the source)."""

    def b64(data: dict[str, str]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'sub': 'attacker'})}."


def check(name: str, condition: object, detail: str = "") -> bool:
    ok = bool(condition)
    RESULTS.append((name, ok))
    suffix = f" :: {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}", flush=True)
    return ok


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def decode_claims(token: str) -> dict[str, object]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(payload))
    return decoded if isinstance(decoded, dict) else {}


def realm_roles(claims: dict[str, object]) -> list[object]:
    realm = claims.get("realm_access")
    if not isinstance(realm, dict):
        return []
    roles = realm.get("roles")
    return list(roles) if isinstance(roles, list) else []


async def fetch_token(client: httpx.AsyncClient, username: str) -> str:
    response = await client.post(
        TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "username": username,
            "password": PASSWORDS[username],
            "scope": "openid",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"no access_token for {username}: {response.text[:200]}")
    return token


async def upload(
    client: httpx.AsyncClient,
    token: str,
    *,
    filename: str,
    content: str,
    visibility: str = "private",
    spoof_user: str | None = None,
) -> httpx.Response:
    headers = bearer(token)
    if spoof_user is not None:
        headers["X-User-ID"] = spoof_user
    return await client.post(
        "/documents",
        headers=headers,
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
        data={"visibility": visibility},
        timeout=60.0,
    )


async def ask(client: httpx.AsyncClient, token: str, question: str) -> httpx.Response:
    return await client.post(
        "/chat",
        headers={**bearer(token), "Cache-Control": "no-cache"},
        json={"question": question, "conversation_id": f"oidc-{RUN}-{uuid.uuid4().hex[:6]}"},
        timeout=120.0,
    )


async def phase_tokens(client: httpx.AsyncClient) -> dict[str, str]:
    print("\n== identity provider ==", flush=True)
    tokens: dict[str, str] = {}
    for username in PASSWORDS:
        try:
            tokens[username] = await fetch_token(client, username)
        except Exception as exc:
            check(f"token for {username}", False, repr(exc)[:200])
        else:
            check(f"token for {username}", True)

    for username, token in tokens.items():
        claims = decode_claims(token)
        subject = str(claims.get("sub", ""))
        audience = claims.get("aud")
        aud_list = [audience] if isinstance(audience, str) else list(audience or [])
        roles = realm_roles(claims)
        check(
            f"{username}: iss matches OIDC_ISSUER",
            claims.get("iss") == ISSUER,
            str(claims.get("iss")),
        )
        check(f"{username}: aud contains {AUDIENCE}", AUDIENCE in aud_list, str(aud_list))
        check(f"{username}: sub present", bool(subject), subject)
        check(
            f"{username}: exp > iat",
            int(claims.get("exp", 0)) > int(claims.get("iat", 0)),  # type: ignore[arg-type]
            f"iat={claims.get('iat')} exp={claims.get('exp')}",
        )
        if username == "operator-1":
            check(
                "operator-1: IdP role support-operator",
                "support-operator" in roles,
                str(roles),
            )
    return tokens


async def phase_unauthenticated(client: httpx.AsyncClient) -> None:
    print("\n== no token / bad token ==", flush=True)
    no_auth = await client.post(
        "/documents",
        files={"file": ("nothing.md", b"# x", "text/markdown")},
        timeout=30.0,
    )
    check("upload without bearer is 401", no_auth.status_code == 401, str(no_auth.status_code))

    # In OIDC mode the header stub is not an identity source at all.
    header_only = await client.get(
        "/documents/00000000-0000-0000-0000-000000000000",
        headers={"X-User-ID": "agent-user"},
        timeout=30.0,
    )
    check(
        "X-User-ID alone is 401 (stub replaced)",
        header_only.status_code == 401,
        str(header_only.status_code),
    )

    garbage = await client.get(
        "/documents/00000000-0000-0000-0000-000000000000",
        headers=bearer("not-a-jwt"),
        timeout=30.0,
    )
    check("malformed token is 401", garbage.status_code == 401, str(garbage.status_code))

    tampered = await client.get(
        "/documents/00000000-0000-0000-0000-000000000000",
        headers=bearer(_unsigned_token()),
        timeout=30.0,
    )
    check("alg=none token is 401", tampered.status_code == 401, str(tampered.status_code))


async def phase_rate_limit(client: httpx.AsyncClient, token: str) -> None:
    """The 21st request must be 429 even when every request spoofs X-User-ID."""
    print("\n== rate limit keyed on the verified sub ==", flush=True)
    zero = "00000000-0000-0000-0000-000000000000"
    statuses: list[int] = []
    templates: dict[str, str] = {}
    for i in range(1, RATE_LIMIT + 2):
        headers = {**bearer(token), "X-User-ID": f"spoof-{RUN}-{i}"}
        response = await client.get(f"/documents/{zero}", headers=headers, timeout=30.0)
        statuses.append(response.status_code)
        if i == RATE_LIMIT + 1:
            templates = {k.lower(): v for k, v in response.headers.items()}

    before = statuses[:RATE_LIMIT]
    last = statuses[RATE_LIMIT]
    check(
        f"first {RATE_LIMIT} spoofed-header requests are not limited",
        all(code == 404 for code in before),
        str(statuses[:5]) + "...",
    )
    check(f"request {RATE_LIMIT + 1} is 429", last == 429, str(last))
    retry_after = templates.get("retry-after", "")
    check("429 carries Retry-After", retry_after.isdigit() and int(retry_after) > 0, retry_after)
    check(
        "429 reports the configured limit",
        templates.get("x-ratelimit-limit") == str(RATE_LIMIT),
        templates.get("x-ratelimit-limit", ""),
    )
    check(
        "429 reports zero remaining",
        templates.get("x-ratelimit-remaining") == "0",
        templates.get("x-ratelimit-remaining", ""),
    )

    wait = int(retry_after) + 2 if retry_after.isdigit() else 62
    print(f"      waiting {wait}s for the fixed window to reset...", flush=True)
    await asyncio.sleep(wait)
    reset = await client.get(f"/documents/{zero}", headers=bearer(token), timeout=30.0)
    check("window resets after Retry-After", reset.status_code == 404, str(reset.status_code))


async def phase_ownership(client: httpx.AsyncClient, agent: str, other: str, operator: str) -> None:
    print("\n== identity, ownership, RBAC ==", flush=True)
    agent_sub = str(decode_claims(agent)["sub"])
    operator_sub = str(decode_claims(operator)["sub"])
    content = (
        "# Refund policy\n\n"
        f"Refund window for a new subscription is 30 days from purchase (run {RUN}).\n"
    )

    # A valid token plus a spoofed header: the token wins.
    uploaded = await upload(
        client,
        agent,
        filename=f"oidc-{RUN}.md",
        content=content,
        spoof_user="other-user",
    )
    check("upload with token is 201", uploaded.status_code == 201, str(uploaded.status_code))
    body = uploaded.json() if uploaded.status_code < 300 else {}
    document_id = body.get("document_id")
    check(
        "owner is the token subject, not the spoofed X-User-ID",
        body.get("owner_id") == agent_sub,
        f"owner_id={body.get('owner_id')} sub={agent_sub}",
    )
    check(
        "upload is private by default",
        body.get("visibility") == "private",
        str(body.get("visibility")),
    )

    if not document_id:
        check("document id returned", False, uploaded.text[:200])
        return

    peek = await client.get(f"/documents/{document_id}", headers=bearer(other), timeout=30.0)
    check("another user cannot read it (404)", peek.status_code == 404, str(peek.status_code))

    deleted = await client.delete(f"/documents/{document_id}", headers=bearer(other), timeout=30.0)
    check(
        "another user cannot delete it (404)", deleted.status_code == 404, str(deleted.status_code)
    )

    # Non-operator cannot publish to the shared corpus.
    denied = await upload(
        client,
        agent,
        filename=f"oidc-public-{RUN}.md",
        content=f"# Shared\n\nShared doc {RUN}.\n",
        visibility="public",
    )
    check(
        "non-operator cannot publish public (403)",
        denied.status_code == 403,
        str(denied.status_code),
    )

    # operator-1 is an operator only through the IdP role (OPERATOR_IDS is empty).
    published = await upload(
        client,
        operator,
        filename=f"oidc-shared-{RUN}.md",
        content=f"# Shared\n\nShared document visible to every caller (run {RUN}).\n",
        visibility="public",
    )
    check(
        "IdP role support-operator may publish public (201)",
        published.status_code == 201,
        str(published.status_code),
    )
    shared_id = published.json().get("document_id") if published.status_code < 300 else None
    if shared_id:
        shared = await client.get(f"/documents/{shared_id}", headers=bearer(other), timeout=30.0)
        check(
            "public document is readable by another user",
            shared.status_code == 200,
            str(shared.status_code),
        )

    # Retrieval is owner-scoped: the same question must refuse for the other user.
    question = "What is the refund window for a new subscription?"
    owner_answer = await ask(client, agent, question)
    owner_body = owner_answer.json()
    check(
        "owner gets a grounded answer",
        owner_answer.status_code == 200 and owner_body.get("refused") is False,
        f"status={owner_answer.status_code} refused={owner_body.get('refused')}",
    )
    check(
        "owner answer cites the uploaded document",
        any("oidc-" + RUN in c.get("source", "") for c in owner_body.get("citations", [])),
        str([c.get("source") for c in owner_body.get("citations", [])]),
    )

    # operator-1 does not own the private doc and no other user may see it.
    outsider = await ask(client, operator, question)
    outsider_body = outsider.json()
    check(
        "another user is refused the private document",
        outsider.status_code == 200 and outsider_body.get("refused") is True,
        f"status={outsider.status_code} refused={outsider_body.get('refused')} "
        f"citations={len(outsider_body.get('citations', []))}",
    )

    # Escalation must stay pending until a *different* operator approves it.
    escalation = await ask(client, agent, "I need to speak to a human about my account, please.")
    escalation_body = escalation.json()
    approval = escalation_body.get("approval") or {}
    approval_id = approval.get("id")
    check(
        "escalation is pending, not auto-approved",
        escalation.status_code == 200 and approval.get("status") == "pending",
        f"status={escalation.status_code} approval={approval}",
    )
    if not approval_id:
        return

    own = await client.post(
        f"/approvals/{approval_id}",
        headers=bearer(agent),
        json={"decision": "approve"},
        timeout=60.0,
    )
    check("requester cannot approve (403)", own.status_code == 403, str(own.status_code))

    peer = await client.post(
        f"/approvals/{approval_id}",
        headers=bearer(other),
        json={"decision": "approve"},
        timeout=60.0,
    )
    check("non-operator cannot approve (403)", peer.status_code == 403, str(peer.status_code))

    decided = await client.post(
        f"/approvals/{approval_id}",
        headers=bearer(operator),
        json={"decision": "approve", "note": f"verified {RUN}"},
        timeout=120.0,
    )
    decided_body = decided.json()
    check("operator approves (200)", decided.status_code == 200, str(decided.status_code))
    check(
        "status is approved",
        decided_body.get("status") == "approved",
        str(decided_body.get("status")),
    )
    check(
        "decided_by is the operator subject",
        decided_body.get("decided_by") == operator_sub,
        str(decided_body.get("decided_by")),
    )
    check(
        "a ticket exists", bool(decided_body.get("ticket_ref")), str(decided_body.get("ticket_ref"))
    )
    check(
        "dispatch did not fail",
        decided_body.get("dispatch_failed") is False,
        str(decided_body.get("dispatch_failed")),
    )

    again = await client.post(
        f"/approvals/{approval_id}",
        headers=bearer(operator),
        json={"decision": "reject"},
        timeout=60.0,
    )
    check("a second decision is 409", again.status_code == 409, str(again.status_code))

    first_ref = decided_body.get("ticket_ref")
    dispatch = await client.post(
        f"/approvals/{approval_id}/dispatch", headers=bearer(operator), timeout=60.0
    )
    dispatch_body = dispatch.json()
    check("dispatch retry is 200", dispatch.status_code == 200, str(dispatch.status_code))
    check(
        "dispatch retry never creates a second ticket",
        dispatch_body.get("ticket_ref") == first_ref,
        f"{first_ref} -> {dispatch_body.get('ticket_ref')}",
    )

    # Separation of duties: an operator cannot approve their own escalation.
    sos = await ask(client, operator, "Please get me a human manager on this.")
    sos_id = (sos.json().get("approval") or {}).get("id")
    if sos_id:
        self_decision = await client.post(
            f"/approvals/{sos_id}",
            headers=bearer(operator),
            json={"decision": "approve"},
            timeout=60.0,
        )
        check(
            "operator cannot self-approve (403)",
            self_decision.status_code == 403,
            f"{self_decision.status_code} {self_decision.text[:120]}",
        )


async def main() -> int:
    print(f"OIDC verification run {RUN}", flush=True)
    print(f"base_url={BASE_URL} token_url={TOKEN_URL}", flush=True)
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=60.0) as client:
        health = await client.get("/health")
        if health.status_code != 200:
            print(f"API is not healthy: {health.status_code} {health.text[:300]}")
            return 2
        providers = health.json().get("providers") or {}
        print(
            f"providers: llm={providers.get('llm')} embedding={providers.get('embedding')}",
            flush=True,
        )

        tokens = await phase_tokens(client)
        if len(tokens) != len(PASSWORDS):
            print("cannot continue without all tokens")
            return 2

        await phase_unauthenticated(client)
        await phase_rate_limit(client, tokens["other-user"])
        await phase_ownership(
            client, tokens["agent-user"], tokens["other-user"], tokens["operator-1"]
        )

    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed", flush=True)
    if failed:
        print("failed checks:", flush=True)
        for name in failed:
            print(f"  - {name}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
