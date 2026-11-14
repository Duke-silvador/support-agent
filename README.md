# Support Agent

> ## ⚠️ Demo/testing only — not production-ready
>
> This repository is a reference implementation used for evaluation. It ships
> with deterministic `fake` providers, a local Keycloak realm and **test-only
> credentials**. Do not expose it to untrusted traffic until the gaps in
> [Known production gaps](#known-production-gaps-not-implemented) are closed:
>
> * **OIDC is local/demo only.** The Keycloak realm is imported from
>   `deploy/keycloak/realm-support-agent.json`, the client uses the password
>   grant, and operator MFA is not configured. Production needs a real IdP,
>   authorisation-code + PKCE, and MFA for operator accounts.
> * **No TLS.** Postgres (`sslmode`), Redis (`rediss://`) and OTLP are plaintext
>   inside the compose network; TLS is expected to terminate at an ingress that
>   does not exist in this repo.
> * **No KMS / secret manager.** Every credential is an environment variable;
>   there is no rotation and no KMS-backed encryption.
> * **No MFA or per-day budgets.** Only a per-subject request window exists
>   (20 req/min); token/embedding budgets and IP-level limits are missing.
> * **Escalation is a stub.** `agents/escalation.py` logs a ticket reference and
>   returns `SUP-…`; wire it to the real ITSM (idempotent on `approval.id`).
> * **Compose credentials are for local testing only** — the Postgres roles
>   (`support_migrator_local_pw`, `support_app_local_pw`), Redis
>   (`support_redis_local_pw`), the Keycloak users (`agent-user`, `other-user`,
>   `operator-1` with `*-local-pw` passwords) and `OPERATOR_IDS=operator-1`.
>   They are published on purpose so the demo runs with one command.

AI agent that answers customer questions from **your** company documents (RAG) and
**cites the source of every answer**, with human-in-the-loop escalation.

```
Client ──POST /chat──► FastAPI ──► Agent loop ──► search_docs (pgvector/HNSW)
                          │            │
                          │            ├──► LLM (fake | openai | anthropic)
                          │            └──► escalate_to_human ──► approvals (pending)
                          ├── Redis: 20 req/min/user + answer cache
                          └── OTel: one trace per chat, one span per LLM/tool call
```

Stack: Python 3.12 · FastAPI · PostgreSQL 16 + pgvector (HNSW) · Redis 7 · OpenTelemetry · Docker Compose.

## Quickstart (one command)

```bash
docker compose up -d --build
curl http://localhost:8000/health
```

| URL | What |
|---|---|
| http://localhost:8000/docs | OpenAPI docs |
| http://localhost:16686 | Jaeger — traces (`chat.request`, `retrieval.search`, `tool.*`) |
| localhost:5433 | PostgreSQL (pgvector) |
| localhost:6380 | Redis |

Every published port (API, Postgres, Redis, Keycloak, Jaeger, OTel collector) is bound to
`127.0.0.1`, so the demo is reachable from your own machine only. The compose passwords are
public test values; **do not change these bindings to `0.0.0.0` or run the stack on a
shared/cloud host without replacing every credential first.**

The default providers are `fake` (deterministic), so the whole stack runs offline with
**no API key**. To use a real model:

```bash
cp .env.example .env
# LLM_PROVIDER=openai
# EMBEDDING_PROVIDER=openai
# OPENAI_API_KEY=sk-...
docker compose up -d
```

`LLM_PROVIDER` and `EMBEDDING_PROVIDER` are separate on purpose: Anthropic serves chat
but **not** embeddings, so `LLM_PROVIDER=anthropic` must be paired with
`EMBEDDING_PROVIDER=openai` (or `fake`).

## API

All endpoints except `/health` require an authenticated identity (see below).

### `POST /documents`

Multipart upload of `.md` / `.txt` (UTF-8, ≤ 5 MB). Chunks, embeds and indexes the
document. Identical content is deduplicated **per owner** (SHA-256), so identical bytes
in two users' documents are two independent rows and never a metadata leak.

Documents are **private to their owner by default**; retrieval only returns a caller's
own documents plus explicitly `public` ones. Publishing to the shared corpus requires
an operator:

```bash
# private document (default)
curl -X POST http://localhost:8000/documents \
  -H "X-User-ID: alice" \
  -F "file=@examples/docs/refund-policy.md"

# shared corpus (operator only)
curl -X POST http://localhost:8000/documents \
  -H "X-User-ID: operator-1" \
  -F "file=@examples/docs/refund-policy.md" \
  -F "visibility=public"
```

```json
{
  "document_id": "…",
  "filename": "refund-policy.md",
  "title": "Refund policy",
  "chunk_count": 4,
  "duplicate": false,
  "injection_flags": [],
  "created_at": "2026-11-05T21:40:00Z",
  "owner_id": "alice",
  "visibility": "private"
}
```

### `GET /documents/{id}` and `DELETE /documents/{id}`

A document is visible to its owner and to operators; `public` documents are readable by
every authenticated caller. Anything else answers `404` (existence is not leaked).
Deleting a private document is owner-or-operator; deleting a `public` document requires
an operator because it is shared context. Chunks are removed by `ON DELETE CASCADE`.

### `POST /chat`

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "X-User-ID: alice" \
  -d '{"question": "How long do I have to request a refund?"}'
```

```json
{
  "answer": "You can request a refund within 30 days of your first charge.",
  "citations": [
    {
      "chunk_id": "…",
      "source": "refund-policy.md",
      "section": "Refund policy",
      "score": 0.71,
      "snippet": "New subscriptions can be refunded within 30 days…"
    }
  ],
  "refused": false,
  "used_tools": ["search_docs"],
  "approval": null,
  "conversation_id": "c-…",
  "model": "fake-extractive-v1",
  "usage": {"input_tokens": 412, "output_tokens": 38},
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "cached": false
}
```

Response headers: `X-Cache: HIT|MISS`, `X-RateLimit-Limit/Remaining/Reset`,
`Retry-After` on `429`. `trace_id` is the active OpenTelemetry trace id, and is an
empty string when `OTEL_ENABLED=false` (e.g. the test image).

### `POST /approvals/{id}` — human-in-the-loop

`escalate_to_human` never creates a ticket. It stores an approval with
`status=pending`; a human decides:

```bash
curl -X POST http://localhost:8000/approvals/<id> \
  -H "Content-Type: application/json" \
  -H "X-User-ID: operator-1" \
  -d '{"decision": "approve", "note": "billing issue"}'
```

The deciding operator is taken from the **verified caller only** — never from the
request body. Operators are an `OPERATOR_IDS` allowlist of subjects and/or members of
the `OIDC_OPERATOR_ROLE` IdP role. An empty allowlist **denies every decision**;
`ALLOW_ANON_OPERATORS=true` opens it for a local demo only, and prod refuses to boot
with either an empty list or that flag set. The compose file ships
`OPERATOR_IDS=operator-1` for the local demo.

* `approve` → the decision **and** a durable outbox row commit in one transaction; a
  ticket is then created (stubbed in `agents/escalation.py`) and `ticket_ref` returned.
* `reject` → no side effect and no outbox row.
* The requester cannot approve their own escalation (`403`), non-operators get `403`, and
  an approval that was already decided cannot be decided again (`409`).
* If the ticket backend fails, the decision still stands: the row stays in
  `escalation_outbox` and `src/support_agent/workers/outbox.py` retries it with
  exponential backoff (`OUTBOX_MAX_ATTEMPTS`, `OUTBOX_BASE_BACKOFF_SECONDS`). The API
  response then carries `dispatch_failed: true` with `ticket_ref: null`.
* Retrying never creates a second ticket: `approval_id` is unique in the outbox and a
  stored `ticket_ref` short-circuits. Operators can force a retry:

```bash
curl -X POST http://localhost:8000/approvals/<id>/dispatch -H "X-User-ID: operator-1"
```

The worker runs as the `worker` compose service (`python -m support_agent.workers.outbox`)
and is safe to scale horizontally: due rows are claimed with `FOR UPDATE SKIP LOCKED`,
and a claim abandoned by a dead worker is reclaimed after `OUTBOX_STALE_SECONDS`.

### `GET /health`

Returns `200` when Postgres and Redis respond, `503` otherwise, plus the active
(non-secret) provider configuration.

## Identity: OIDC (Keycloak locally) or the header stub

`AUTH_MODE` selects the identity source:

| Mode | Use | How identity is derived |
|---|---|---|
| `header` | local dev only | `X-User-ID` header. **Spoofable by design**; prod refuses to boot with it. |
| `oidc` | production / local Keycloak | `Authorization: Bearer <JWT>`. Signature is verified against the provider's JWKS, then `iss`, `aud`, `exp`/`nbf`/`iat` (with leeway) and a required `sub`. The user id is the verified `sub`; `X-User-ID` is ignored. |

Algorithm allowlist is fixed to `RS*`/`ES*` (never `HS*`, never `none`). Roles are read
from Keycloak's `realm_access.roles`; membership of `OIDC_OPERATOR_ROLE` grants operator
powers. Rate limiting is keyed on the verified subject.

Signing keys come from the issuer's discovery document
(`<OIDC_ISSUER>/.well-known/openid-configuration`) unless `OIDC_JWKS_URI` is set
explicitly. That matters in practice: Keycloak publishes its keys at
`/protocol/openid-connect/certs`, not at a guessed `/.well-known/jwks.json`.

Failure modes are intentionally distinct:

| Condition | Response |
|---|---|
| missing/expired/bad-signature/bad-claim token, or a header without `kid` (including `alg=none`) | `401` |
| unknown `kid` after a key-set refresh, a malformed/unparseable JWKS, or the JWKS/discovery endpoint being unreachable | `503` (retryable, not a bypass) |

Local Keycloak (realm `support-agent`, users `agent-user`, `other-user`, `operator-1`):

```bash
docker compose --profile auth up -d keycloak
# set AUTH_MODE=oidc and restart the app
docker compose up -d app worker
# token (password grant is for local testing only)
curl -s -X POST http://localhost:8081/realms/support-agent/protocol/openid-connect/token \
  -d grant_type=password -d client_id=support-agent-api \
  -d username=agent-user -d password=agent-user-local-pw | jq -r .access_token
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/health
```

The whole OIDC path is verified end-to-end by `scripts/verify_oidc.py` (tokens →
`sub` identity → ownership isolation → `sub`-keyed rate limit → IdP operator role →
outbox ticket). It must run **inside** the compose network, because tokens are minted
from `http://keycloak:8080` so the `iss` claim matches `OIDC_ISSUER`. `OPERATOR_IDS`
is deliberately empty so the IdP role is the *only* way to be an operator:

```bash
docker compose --profile auth up -d --wait keycloak
docker compose run -d --name oidc-app --no-deps --service-ports \
  -e AUTH_MODE=oidc -e OTEL_ENABLED=false -e OPERATOR_IDS= app
docker compose --profile test run --rm --no-deps -v ./scripts:/scripts:ro \
  -e OIDC_VERIFY_BASE_URL=http://oidc-app:8000 \
  test python /scripts/verify_oidc.py
```

It prints `PASS`/`FAIL` per check and exits non-zero if any check fails.

For production, terminate TLS at the ingress (NGINX/Gateway API) with a WAF in front,
use authorization-code + PKCE instead of the password grant, and require MFA for
operator accounts.

## Security model

| Risk | Control |
|---|---|
| Prompt injection from documents | Documents are wrapped in `<<<UNTRUSTED_DOC … n="nonce">>>` blocks with a **per-request nonce**; the system prompt (priority rules) says content inside is data, never instructions. A forged closing tag cannot match because the nonce is unguessable and the parser requires a matching back-reference. |
| Injection detection | `core/security.py` flags instruction overrides, prompt probes, role hijack, tool invocation, exfiltration. Flags are **stored as metadata**; the text is **never rewritten**. Surfaced in the upload response and traces. |
| Hallucinated citations | `agents/verifier.py` drops any citation id not returned by retrieval. An answer with no surviving citation is replaced by the canonical refusal. |
| Excessive agency | Exactly two tools. `search_docs` = one parameterised SELECT. `escalate_to_human` = insert a *pending* row. No shell, no HTTP, no writes. Tool arguments are Pydantic-validated; unknown tools are rejected. |
| Escalation decisions | Operator allowlist (`OPERATOR_IDS`) and/or IdP operator role (`OIDC_OPERATOR_ROLE`), empty = deny; decision identity from the verified caller, never the body; requester cannot self-approve; one-shot decisions (`409`). |
| Escalation durability | Decision + outbox row commit atomically; a retrying worker dispatches with exponential backoff; unique `approval_id` + stored `ticket_ref` make retries idempotent (no double ticket). |
| Document isolation (tenant) | Every document has a verified `owner_id` and `private`/`public` visibility. Retrieval, `GET` and `DELETE` filter at the SQL layer (`public OR owner`), unknown ids collapse to `404`, publishing/deleting shared docs requires an operator. The answer cache key includes the viewer, so a cached answer can never cross users. |
| Database privilege | API and worker connect as `support_app` (DML only, no DDL, no role creation); Alembic uses `support_migrator`, which owns the schema. A leaked app credential cannot alter or drop tables. |
| Datastore auth | Postgres requires a password; Redis uses `requirepass` and `maxmemory-policy noeviction` so limiter keys are never silently evicted. |
| Upload / body DoS | Every body-bearing request is size-capped by a chunked-safe ASGI byte counter before Starlette spools the multipart body or reads the body into memory (`MAX_UPLOAD_BYTES` for `/documents`, `MAX_JSON_BYTES` otherwise, regardless of `Content-Type`); the upload route re-checks the exact payload size and rejects documents needing more than `MAX_CHUNKS_PER_DOCUMENT` embeddings. |
| PII in telemetry | Spans carry a truncated SHA-256 user hash, not the raw id; the collector redacts prompt/document attributes and the debug exporter is off. |
| Cost runaway | 20 req/min/user, 4-iteration cap, one search per chat, `max_output_tokens`, answer cache. Rate limiter **fails closed** (503) if Redis is down. |
| Secrets | Environment only. `.env` is git-ignored; `.env.example` documents every knob with no values. No provider key is logged; the OTel collector deletes `llm.prompt`/`llm.completion` attributes. |
| Transport | TLS terminated at ingress (not in compose; local HTTP only). |

## Observability

OpenTelemetry (OTLP/gRPC → collector → Jaeger, with the collector also exposing a
Prometheus scrape endpoint on `:8889`):

> Traces are the primary signal. The meter provider and the collector's Prometheus
> exporter are wired, but the app does not yet record custom counters/histograms —
> treat `/metrics` as plumbing, not a dashboard.

```
chat.request                 user hash, conversation, question size, refused,
                             citations, dropped_citations, tools, tokens, iterations
├── llm.completion           provider, model, iteration, tokens, finish_reason
├── tool.search_docs         ok, attempt  (retrieval.search nested)
│   └── retrieval.search     top_k, min_score, returned, top score, chunk ids
├── tool.escalate_to_human   ok, attempt, approval.id
└── guardrail.citations      claimed, kept, dropped, refused
```

Prompts and document bodies are **not** exported as attributes. Every `/chat` response
contains its `trace_id` so support can jump straight to the trace.

## Tests and evals

```bash
# unit only — no containers needed
pytest -q -m "not integration"

# full suite inside the test image (starts postgres + redis, runs migrations)
make test-all
# or directly:
docker compose --profile test run --rm test

# integration tests against an already-running stack, from the host
RUN_INTEGRATION=1 pytest -q -m integration

# behavioural evals against a running stack
python -m evals.runner          # EVAL_BASE_URL defaults to http://localhost:8000
```

`RUN_INTEGRATION=1` is required for integration tests when using plain `pytest`;
without it (or `-m integration`) they are reported as skipped.

Eval cases (`evals/cases.yaml`) cover: answering from documents, refusal outside the
documents, citation accuracy, injection resistance, and escalation staying pending.

> **Important — fake mode is plumbing, not quality.** With `LLM_PROVIDER=fake`, evals
> verify the pipeline contract (retrieval → prompt assembly → citation verification →
> refusal → approval). They say nothing about retrieval or answer quality. Quality
> numbers require a real model and a labelled question set; treat fake-mode results as
> a regression gate only.

Eval fixtures are prefixed `eval-` and `citations_from` requires **all** listed source
names to appear in the citations, so a pass proves the answer came from the eval corpus.
The runner sends `Cache-Control: no-cache`, so reruns inside the cache TTL still exercise
retrieval and the model. The runner ingests documents **as the same eval user** that
asks the questions: uploads are private and retrieval is owner-scoped (`public OR owner`),
so a separate ingestion identity would make every in-scope question refuse. Run it against
a fresh database (CI does) — documents already in a dev database compete for retrieval and
can change rankings.

## CI/CD

`.github/workflows/ci.yml` (every Action is pinned to a commit SHA; the comment on
each line is the human-readable tag):

1. **lint + types + audit** — installs from the hashed lockfile, then `ruff check`,
   `ruff format --check`, `mypy` (strict), and **blocking** `pip-audit` against both
   `requirements.lock` and `requirements-dev.lock` (`--require-hashes --strict`).
2. **unit + integration + evals** — Postgres/pgvector + Redis service containers,
   Alembic migrations, both test selections, then the eval runner against a live Uvicorn.
3. **image scan + SBOM** — builds the runtime image, runs **Trivy** (`HIGH,CRITICAL`,
   `ignore-unfixed`, blocking `exit-code 1`) with a JSON report artifact, and produces
   both **SPDX 2.3** and **CycloneDX** JSON SBOMs via Syft (`anchore/sbom-action`).

### Dependencies: hashed lockfiles

`pyproject.toml` holds the ranges; the *installed* set is frozen in two fully pinned,
hash-carrying lockfiles:

| File | Contents | Used by |
|---|---|---|
| `requirements.lock` | runtime dependencies | runtime/test images |
| `requirements-dev.lock` | runtime + `[dev]` extras | CI, test image, local dev |

Both are installed with `pip install --no-deps --require-hashes`. The Docker build can
additionally pass `--only-binary :all:` so an sdist is never unpacked and executed
during the build. Regenerate after changing `pyproject.toml`:

```bash
# uv 0.13.x (no pip-tools: it cannot import pip>=25). --upgrade is required:
# without it uv reuses the existing lock as a preference and keeps stale pins.
uv pip compile pyproject.toml --generate-hashes --python-version 3.12 --upgrade \
  -o requirements.lock
uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 \
  --upgrade -o requirements-dev.lock
# or simply:
make lock
make audit           # blocking pip-audit (runs in Docker) against both locks
docker compose build # verify the locks install in the image
```

The app is imported from `src/` (`PYTHONPATH=/app/src` in the images,
`pythonpath = ["src"]` for pytest), so no wheel is built during CI or image builds and
the dependency set is exactly the lockfile.

### Local security scans

```bash
make scan            # Trivy image scan (HIGH/CRITICAL, blocking)
make sbom            # Syft -> sbom.spdx.json + sbom.cdx.json
```

The scanner images (`aquasec/trivy`, `anchore/syft`) are pinned by tag **and**
digest in the `Makefile`, so a moved tag cannot change what the gate runs. The
application base image (`python:3.12-slim`) is digest-pinned in the `Dockerfile`
for the same reason; the bump procedure is in a comment next to the `FROM`. The
build also applies the current Debian security updates (`apt-get upgrade`), so a
base digest that predates a Debian point release does not ship a known-fixed CVE:
the Trivy gate fails on HIGH/CRITICAL **with a published fix**, and it is run on
every build in CI.

## Layout

```
src/support_agent/
├── api/         routes (chat, documents, approvals, health), schemas, deps
├── agents/      loop, prompts, tools, verifier, escalation
├── rag/         chunker, ingestion, retriever (pgvector/HNSW)
├── providers/   base protocols, fake, openai, anthropic, factory
├── core/        config, logging, telemetry, cache, ratelimit, security, identity
└── db/          models, session, migrations (Alembic)
tests/           unit + integration
evals/           cases.yaml, runner.py, data/
scripts/         audit.sh (pip-audit), verify_oidc.py (live Keycloak gate)
deploy/          otel-collector config, postgres init, Keycloak realm
```

## Operations notes

* Migrations run as a one-shot `migrate` service before the app starts; the app never
  migrates itself.
* `EMBEDDING_DIM` is baked into the vector column. Changing it needs a new migration
  and a re-index.
* HNSW params (`m=16`, `ef_construction=64`) are pgvector defaults for tens of
  thousands of chunks; tune with real data.
* Escalation is intentionally a stub (`agents/escalation.py` logs a ticket reference).
  Wire it to your ITSM/Zendesk/queue before production.
* `OPERATOR_IDS` (comma-separated) is mandatory when `ENVIRONMENT=prod` — the app
  refuses to boot without it and rejects `ALLOW_ANON_OPERATORS=true`. An empty list
  denies decisions everywhere; `ALLOW_ANON_OPERATORS=true` is local-dev only. The
  deciding operator is the verified caller id, never a request-body field.

### Known production gaps (not implemented)

These are deliberate, documented gaps — the app is not production-ready until they are
closed in the deployment:

* **Knowledge-base writes are authorized per owner.** Uploads are private by default,
  publishing to the shared corpus requires an operator, and retrieval/GET/DELETE filter
  at SQL level. Residual: quarantining documents with `injection_flags` until reviewed.
* **Identity is OIDC-ready; limits are per verified subject.** Residual: per-day
  token/embedding budgets and per-IP abuse limits.
* **Datastores are password-protected but still plaintext in the cluster.** Require
  `rediss://` / `sslmode=require`, KMS-backed secrets and rotation outside local dev.
  Runtime roles are already least-privilege (`support_app` cannot DDL).
* **Escalation dispatch is a stub** (`agents/escalation.py`) behind a durable outbox +
  retrying worker. Replace the stub with the real ITSM call (idempotent on `approval.id`).
* **MFA for operator accounts is not implemented.** It belongs at the IdP (realm
  policy); the application only checks the `OIDC_OPERATOR_ROLE`/`OPERATOR_IDS`
  allowlist.
* **The demo credentials in `docker-compose.yml` are test-only.** Postgres
  (`support_migrator_local_pw`, `support_app_local_pw`), Redis
  (`support_redis_local_pw`), the Keycloak realm users (`*-local-pw`) and
  `OPERATOR_IDS=operator-1` exist so the stack starts with one command. Production must
  inject them from a secret manager and must not reuse these values.
* **Answer quality was evaluated with `fake` providers only.** The evals prove the
  pipeline contract, not retrieval or answer quality; run them against a real model
  before quoting quality numbers.
* **`AUTH_MODE=header` is the local default.** It trusts `X-User-ID`, so it is a
  complete auth bypass if exposed; the app logs a WARN at startup and compose binds
  the port to `127.0.0.1` only. Any real deployment must set `AUTH_MODE=oidc` (prod
  refuses to boot otherwise) and should front the API with an ingress that strips
  `X-User-ID`.
* **No pre-auth rate limit.** The limiter runs after identity resolution, so forged
  tokens still cost a JWKS lookup/signature check; an unknown `kid` triggers a JWKS
  refresh. Production needs an IP/route limiter in front of the API plus a negative
  `kid` cache.
* **The bundled Keycloak realm is a demo.** `sslRequired: none`, a public client with
  the password grant and no brute-force protection or MFA. Import a hardened realm
  for anything but local use.
* **`/health` performs datastore round-trips** and reports `environment`/provider
  names; restrict it at the ingress (or split liveness/readiness) in production.
