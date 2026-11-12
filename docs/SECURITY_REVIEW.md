# Security & correctness review — findings register

Scope: `support-agent` (FastAPI + PostgreSQL/pgvector + Redis + OpenTelemetry RAG agent).
Sources: two independent static audits (`@security`, `@reviewer`) of the code at
`C:\dev\support-agent`, plus the fixes implemented afterwards.

Status values:

* **FIXED** — code changed and covered by a unit/integration test or a reproducible command.
* **PARTIAL** — the reported risk is reduced, but a named residual remains.
* **OPEN (gap)** — not implemented; documented as a production gap.
* **REJECTED** — accepted as-is, with the reason recorded.

This file is updated after every review round. The final section records the last verdict.

---

## 1. `@security` findings (round 2)

| ID | Severity | Finding | Status | Resolution / reason |
|---|---|---|---|---|
| S-H1 | High | Operator authorization failed open whenever `ENVIRONMENT != prod`; the shipped compose file was such a deployment. | **FIXED** | `is_operator()` denies an empty allowlist; `ALLOW_ANON_OPERATORS=true` is explicit local-dev only; `Settings` refuses `prod` with an empty list or the anon flag. Compose ships `OPERATOR_IDS=operator-1`. Tests: `tests/test_config.py::test_prod_rejects_anonymous_operators`, `::test_only_allowlisted_operators_may_decide`; integration `403/404/422` cases. |
| S-H2 | High | No authorization/ownership on knowledge-base writes; any caller could poison the shared corpus (OWASP LLM01/LLM04). | **FIXED** | `documents.owner_id` + `visibility`; uploads are private to the caller unless an operator publishes (`visibility=public` → `403` otherwise); retrieval, `GET` and `DELETE` filter `public OR owner` in SQL; duplicates are matched per owner. Tests: `tests/test_integration_api.py::test_private_document_is_isolated_between_users`, `::test_only_an_operator_may_publish_a_shared_document`, `::test_duplicate_bytes_do_not_leak_another_owners_document`, `::test_answer_cache_is_scoped_to_the_viewer`. |
| S-H3 | High | Upload size guard only checked `Content-Length`; chunked uploads could spool unbounded data to disk. | **FIXED** | `BodySizeLimitMiddleware` counts received bytes (chunked-safe) and caps **every** body-bearing method regardless of `Content-Type` (the header is attacker-controlled). Tests: `tests/test_upload_limit.py` (declared length, chunked upload, chunked JSON, text/plain bypass, replay). |
| S-H4 | High | Rate limits keyed on an attacker-controlled header (`X-User-ID`), so rotating it bypasses all cost limits. | **PARTIAL** | In `AUTH_MODE=oidc` the limiter runs after `get_identity` and keys on the verified `sub` from the bearer token (`api/deps.py::enforce_rate_limit` → `core/identity.py`), so spoofed `X-User-ID` is ignored (`tests/test_auth.py::test_bearer_token_subject_is_the_identity`). Header mode is local-only and prod refuses to boot with it. Residual: no per-IP/global pre-auth budget (see §3). |
| S-H5 | High | Supply chain: no lockfile, non-blocking audit, tag-pinned Actions, no image scan. | **FIXED** | `requirements.lock` / `requirements-dev.lock` generated with `uv ... --generate-hashes` and installed with `--no-deps --require-hashes --only-binary :all:`; CI runs blocking `pip-audit --require-hashes --strict` on both locks; Actions pinned by commit SHA; Trivy image scan (HIGH,CRITICAL, `--ignore-unfixed`, blocking) and Syft SPDX + CycloneDX SBOMs uploaded as artifacts. Evidence in §4. |
| S-M1 | Medium | Approved escalation could end with no ticket and no recovery path. | **FIXED** | Decision + outbox row commit atomically; `process_outbox` dispatches inline and a worker retries with exponential backoff; `POST /approvals/{id}/dispatch` is an idempotent operator retry. Unique `approval_id` + stored `ticket_ref` make a second ticket impossible. Tests: `tests/test_integration_api.py::test_dispatch_failure_is_surfaced_and_retryable`, `::test_outbox_retries_with_backoff_until_one_ticket_exists`, `::test_rejected_escalation_creates_no_ticket`. |
| S-M2 | Medium | Escalation could be requested twice after a timeout post-commit. | **FIXED** | `ToolContext.escalation_attempted` is set before the handler and is authoritative across iterations; tool errors roll the session back. Tests: `tests/test_tools.py::test_escalation_is_never_requested_twice`, `::test_escalation_timeout_after_commit_blocks_a_retry_and_rolls_back`. |
| S-M3 | Medium | Unbounded parallel tool calls in one model turn. | **PARTIAL** | Per-call permission recomputation limits execution to one search and one escalation per chat. Residual: many *parallel failing* `search_docs` calls in the same turn can each run an embedding + query. A hard per-request execution cap remains a gap. |
| S-M4 | Medium | PII in logs/traces; OTLP exported without TLS; debug exporter retained attributes. | **PARTIAL** | Spans now carry `chat.user_hash` (see `chat.request` in the trace tree); collector redacts prompt/completion attributes and the debug exporter was removed. Residual: logs still contain `user_id`, and OTLP TLS is not configurable. |
| S-M5 | Medium | Answer cache had no user/tenant dimension; poisoning amplified for the TTL. | **FIXED** | Cache key includes `viewer_id` in addition to question, corpus version and provider/model; retrieval itself is owner-scoped. Tests: `tests/test_cache.py::test_cache_key_is_scoped_to_the_viewer`, `tests/test_integration_api.py::test_answer_cache_is_scoped_to_the_viewer` (A hits, B misses). |
| S-M6 | Medium | Redis has no auth/TLS and uses `allkeys-lru` (limiter keys evictable); Postgres has default credentials and the app runs as DDL owner. | **PARTIAL** | Postgres and Redis passwords come from env (`--requirepass`, `REDISCLI_AUTH`); `maxmemory-policy noeviction`; the API/worker run as `support_app` (DML only) while Alembic runs as `support_migrator` (schema owner). Test: `tests/test_integration_roles.py::test_application_role_cannot_run_ddl`. Residual: TLS (`rediss://`, `sslmode=require`), secret manager and rotation are deployment requirements. |
| S-M7 | Medium | A corrupt cache entry became a sticky 500. | **FIXED** | Invalid entries are deleted and treated as a miss. Tests: `tests/test_cache.py::test_corrupt_entry_is_a_miss`, `::test_delete_removes_a_poisoned_entry`; integration `test_cache_hit_reissues_the_callers_conversation_id`. |
| S-M8 | Medium | Prompt-fence residuals: `_safe_attr` missed C0/Cf/bidi characters; 32-bit nonce; document text could copy a marker verbatim. | **FIXED** | `_safe_attr` drops `Cc`/`Cf`/`Cs` characters; the nonce is now 128-bit (`secrets.token_hex(16)`); `render_context` rewrites look-alike `<<<UNTRUSTED_DOC` markers inside document text. Tests: `tests/test_prompts.py::test_nonce_is_128_bits`, `::test_document_cannot_copy_the_real_nonce_marker`, `::test_attributes_drop_control_and_format_characters`. Citation verification still proves provenance, not entailment — documented limitation. |
| S-M9 | Medium | No rate limiting on `/approvals/*`. | **FIXED** | `enforce_rate_limit` added to GET, POST decision, and dispatch routes. |
| S-L1 | Low | `ApprovalResponse` returns `user_id`/`decided_by` to the requester. | **REJECTED** | The requester is entitled to their own escalation, and `decided_by` is required for audit. UUIDs are unguessable and GET is 404 for non-owners/non-operators. |
| S-L2 | Low | `ApprovalDecision` silently ignored a forged `decided_by`. | **FIXED** | `extra="forbid"`; the integration test now asserts `422`. |
| S-L3 | Low | `/health` discloses environment, version, providers, and failing dependency. | **PARTIAL** | Providers are config, not secrets, and the endpoint is needed for local operations. Prod should restrict exposure at the ingress; tracked. |
| S-L4 | Low | Duplicate upload returned another uploader's document metadata. | **FIXED** | Duplicate detection is restricted to `content_sha256 + owner_id`; an identical *public* document now produces the caller's own private copy instead of returning someone else's id/metadata. Test: `tests/test_integration_api.py::test_duplicate_of_a_public_document_stays_private`. |
| S-L5 | Low | No security headers / `TrustedHostMiddleware`. | **PARTIAL** | Security headers are planned at the ingress (WAF/TLS/Ingress). API-only. |
| S-L6 | Low | Collector `debug` exporter retained span attributes. | **FIXED** | Exporter removed from the traces pipeline. |
| S-L7 | Low | Non-prod ports published with default credentials. | **PARTIAL** | Passwords now come from env; compose ports remain for local development only. |
| S-L8 | Low | Test gaps (chunked upload, header-rotation limits, cache cross-user, dispatch failure). | **FIXED** | Added tests for chunked uploads, operator authorization, dispatch failure/retry, no-cache, tool rollback, and escalation dedupe. Header-rotation limits are covered by the OIDC design (verified `sub`). |

---

## 2. `@reviewer` findings (round 2)

| ID | Severity | Finding | Status | Resolution / reason |
|---|---|---|---|---|
| R-H1 | High | Tool timeout/exception left the session un-rolled-back; escalation could run twice. | **FIXED** | `rollback()` in both exception paths; `escalation_attempted` set before the handler. Tests: `tests/test_tools.py` (3 tests). |
| R-H2 | High | Documented "full suite" commands skipped integration tests and migrations. | **FIXED** | `RUN_INTEGRATION=1` supported; compose test service runs `alembic upgrade head && pytest -q`; `make test-all` uses it; README updated. Evidence: `145 passed` in the test image (§4.3). |
| R-H3 | High | JSON endpoints had no body-size cap (memory DoS before auth/rate limit). | **FIXED** | `BodySizeLimitMiddleware` caps **every** body-bearing request: `MAX_UPLOAD_BYTES` for `/documents`, `MAX_JSON_BYTES` (default 1 MiB) for everything else, regardless of the client-supplied `Content-Type`. Tests: `tests/test_upload_limit.py`. |
| R-M1 | Medium | DB connection held during LLM round-trips after retrieval. | **FIXED** | `session.rollback()` after retrieval; `corpus_version()` transaction is closed before the model call. Test: `tests/test_tools.py::test_search_releases_the_read_transaction`. |
| R-M2 | Medium | `dispatch_failed` invisible and not retryable. | **FIXED** | Field added to `ApprovalResponse`; idempotent operator retry route; integration test covers failure → retry → idempotent second retry. |
| R-M3 | Medium | `session.refresh()` after commit opened a transaction during dispatch. | **FIXED** | Refreshes removed; dispatch runs outside the decision transaction. |
| R-M4 | Medium | Eval reruns inside the TTL hit the cache and failed. | **FIXED** | Runner sends `Cache-Control: no-cache`; `/chat` bypasses the read cache for that header. Integration test added. |
| R-M5 | Medium | Eval expectations could be empty/typed wrong; `citations_from` was an intersection. | **FIXED** | Runner validates value types, `citations_from` is all-of, the injection case asserts forbidden outcomes only, and a zero-result run exits non-zero. Evidence: `8/8 cases passed`. |
| R-M6 | Medium | Only embedding dimension was checked at startup; swapping models with the same dimension silently invalidates vectors. | **OPEN (gap)** | Requires storing model/version metadata and a re-index runbook. Documented. |
| R-M7 | Medium | Shared corpus writable by anyone; no role for ingestion. | **FIXED** | Uploads default to `private` with the verified caller as `owner_id`; only operators may set `visibility=public`; retrieval filters `public OR owner`; operators may audit-read and delete shared docs. Same tests as S-H2. |
| R-M8 | Medium | Every exception became a 502 "provider unavailable". | **FIXED** | Only OpenAI/Anthropic/httpx/timeout errors map to 502; other errors surface as 500. |
| R-M9 | Medium | No lockfile; `pip-audit` non-blocking. | **FIXED** | Hashed lockfiles committed and used by CI/images; `pip-audit --require-hashes --strict` fails the build on any known advisory. Evidence in §4. |
| R-M10 | Medium | Telemetry/provider teardown incomplete. | **PARTIAL** | Tracer/meter providers are reset after shutdown. Provider SDK clients are not explicitly closed; the process teardown closes them. |
| R-L1 | Low | POST returns 403 for existing approvals and 404 for unknown ones. | **REJECTED** | Operator authorization must be explicit (`403`); unknown ids are `404`. UUIDs make enumeration impractical. |
| R-L2 | Low | `ENVIRONMENT` defaults to `local`. | **PARTIAL** | Prod validation is strict once `ENVIRONMENT=prod`; the default is intentional for one-command local startup. Deployment must set `ENVIRONMENT=prod`. |
| R-L3 | Low | Middleware replayed `http.disconnect` after the body. | **FIXED** | Replay now terminates with an empty `http.request`. Test asserts it. |
| R-L4 | Low | Cache key omitted prompt/retrieval parameters; `CACHE_TTL_SECONDS=0` still served hits. | **FIXED** | The key includes `PROMPT_VERSION` plus `retrieval_top_k`/`retrieval_min_score`/`agent_max_iterations`/`max_output_tokens` and the prefix is `v2`; `get_cached(ttl_seconds<=0)` is a miss. Tests: `tests/test_cache.py::test_cache_key_changes_with_prompt_or_retrieval_settings`, `::test_ttl_zero_also_disables_reads`. |
| R-L5 | Low | Escalation answers are always replaced by the canonical refusal. | **REJECTED** | Intentional: every answer must be citation-grounded. The escalation itself is surfaced in `approval`; answering with uncited text would violate the guardrail. |
| R-L6 | Low | A second search is refused but still consumes an iteration. | **REJECTED** | Deliberate strict progression (search → optional escalation → answer) bounds cost. |
| R-L7 | Low | Dead code (`ApprovalStatus`, `as_dict`, `is_suspicious`, `get_meter`, `reset_providers`). | **OPEN (gap)** | Cosmetic; removal is tracked. |
| R-L8 | Low | `mypy` only checks `src/`, not tests/evals. | **OPEN (gap)** | Strict typing of tests is a follow-up. |
| R-L9 | Low | README drift (env knobs, span attributes, operator semantics, "full suite"). | **FIXED** | README updated: span tree, env example, operator rules, test commands, cache/eval notes, retry endpoint. |
| R-L10 | Low | Test gaps (lifespan, dispatch failure, tool timeout, approval retry). | **FIXED** | Tool timeout/rollback, dispatch retry, no-cache, and ownership tests added. |

---

## 2b. Round 3 — public-release review (independent agents, final tree)

| ID | Severity | Finding | Status | Resolution |
|---|---|---|---|---|
| S3-1 | High | Body cap bypassable by sending a non-JSON `Content-Type` (uncapped buffer before auth). | **FIXED** | `_limit_for` now caps every `POST/PUT/PATCH`; `tests/test_upload_limit.py::test_non_json_bodies_are_capped_by_content_not_content_type`. |
| S3-2 | High | `AUTH_MODE=header` is the shipped default and compose published `0.0.0.0:8000`; `X-User-ID: operator-1` is an admin bypass. | **PARTIAL** | Startup WARN when header mode is active; compose binds `127.0.0.1:8000:8000`; prod still refuses non-OIDC. Residual: header mode remains the local default by design (documented in README). |
| S3-3 | Medium | Operator role trusted `roles`/`groups` claims; no `azp`/token-type binding. | **FIXED** | `extract_roles` reads only `realm_access.roles`; `verify()` rejects a foreign `azp`/`client_id` and `typ=ID`/`token_use=id`. Tests: `tests/test_auth.py::test_roles_are_collected_only_from_keycloak_realm_roles`, `::test_token_for_another_client_is_rejected`, `::test_identity_token_is_rejected`. |
| S3-4 | Medium | No pre-auth rate limit; unknown `kid` forces a JWKS refresh per request. | **OPEN (gap)** | Requires a Redis IP/route limiter in middleware plus a negative `kid` cache. Documented in §3. |
| S3-5 | Medium | One upload can cost ~2,500 embeddings; `no-cache` is a free cost bypass (no budgets). | **PARTIAL** | `MAX_CHUNKS_PER_DOCUMENT` (default 500) rejects before embedding; per-subject daily token/embedding budgets remain a gap (§3). |
| S3-6 | Medium | Local realm allows plaintext + password grant, no brute-force/MFA. | **OPEN (gap)** | Demo realm only; production must import a hardened realm (documented in README). |
| S3-7 | Low | `/health` does datastore I/O and publishes environment/providers. | **OPEN (gap)** | Restrict at the ingress or split liveness/readiness in the deployment (§3). |
| R3-1 | Medium | `CACHE_TTL_SECONDS=0` still served reads; key ignored prompt/retrieval settings. | **FIXED** | See R-L4 above. |
| R3-2 | Medium | Outbox dispatch is at-least-once: a batch slower than `OUTBOX_STALE_SECONDS` can dispatch twice. | **OPEN (gap)** | Dispatcher must be idempotent on `approval_id`; documented in §3 and in the escalation stub. |
| R3-3 | Low | Fake provider sent a `<10` char escalation summary for terse prompts. | **FIXED** | `summary` is now `f"User asked: {question}"`; covered by the escalation integration cases. |
| R3-4 | Low | `_safe_attr` kept C0/Cf characters (bidi/zero-width). | **FIXED** | `tests/test_prompts.py::test_attributes_drop_control_and_format_characters`. |
| R3-5 | Low | Chunker could emit a chunk larger than `size` when `overlap > size/2`. | **FIXED** | Loop re-slices until within budget; `tests/test_chunker.py::test_high_overlap_never_produces_oversized_chunks`. |
| R3-6 | Low | Migration `0002` downgrade dedupe could leave equal-timestamp duplicates. | **OPEN (gap)** | Downgrade-only path; not exercised by `upgrade head`. Tracked. |

---

## 3. Production gaps still open

These are not defects in the local/demo configuration; they are deployment
requirements that must be closed before public production:

1. TLS for Postgres/Redis (`sslmode=require`, `rediss://`), KMS/secret manager and
   credential rotation. Credentials are env-driven today, not KMS-backed.
2. Per-subject daily token/embedding budgets and IP-level abuse limits (only a
   per-subject request window exists).
3. Embedding-model/version metadata and a re-index runbook.
4. WAF / TLS / Gateway / Ingress manifests, and `/health` restricted at the edge.
5. OTLP export is plaintext inside the cluster; mTLS to the collector is not configured.
6. OIDC operator accounts should additionally require MFA at the IdP (realm policy, not code).
7. The third-party images in `docker-compose.yml` (Postgres, Redis, Jaeger, the
   collector, Keycloak) are pinned by tag, not digest. That is acceptable for local
   development; production manifests must pin digests and be refreshed by a bot.
   The application image and both scanner images *are* digest-pinned.
8. Outbox dispatch is at-least-once across one narrow crash window: if the process
   dies *after* the external ticket call returns but *before* the commit that
   stores `ticket_ref`, the retry calls the ticket system again. The database
   paths are exactly-once and tested; the real ITSM integration must additionally
   accept an idempotency key derived from `approval.id`.

---

## 4. Verification evidence (Kundi A–D)

Every command below was run on this host against the final tree; raw output is
quoted or summarised with exit codes. "Verified" means the command was executed
here, not inferred from the code.

### 4.1 Kundi A — behaviour that was previously only claimed

| Check | Command | Result |
|---|---|---|
| 21st request in a window is limited | 21 × `POST /chat` as one user | `codes=200×20,429`; the 429 carried `retry-after: 56`, `x-ratelimit-limit: 20`, `x-ratelimit-remaining: 0` |
| Answer cache | same question twice, `X-Cache` observed | first call `x-cache: MISS` / `cached=false`, second `x-cache: HIT` / `cached=true` |
| Prod refuses an anonymous operator | `pytest tests/test_config.py -k prod` and a direct boot | `test_prod_requires_an_operator_allowlist` + `test_prod_rejects_anonymous_operators` PASSED; boot prints `REFUSED: Value error, ALLOW_ANON_OPERATORS must be false in prod` |

### 4.2 Dependency CVEs and the lockfiles

The first audit round failed: the previously pinned FastAPI/Starlette/OTel and
pytest versions had published advisories (GHSA-xcq7-p3pf-p88h, the Starlette
`PYSEC-2026-*` multipart/range advisories, `PYSEC-2026-1845` in pytest). Pins were
raised and both locks regenerated:

```
uv 0.13.7 pip compile pyproject.toml --generate-hashes --python-version 3.12 --upgrade -o requirements.lock
uv 0.13.7 pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 --upgrade -o requirements-dev.lock
```

`--upgrade` is required: without it uv treats the existing lock file as a
preference and silently keeps the old (vulnerable) pins even after
`pyproject.toml` changes. `make lock` and the README record this.

Audit result, run twice — locally and inside `python:3.12-slim`
(`scripts/audit.sh`, the same script CI calls):

```
No known vulnerabilities found   (requirements.lock, exit 0)
No known vulnerabilities found   (requirements-dev.lock, exit 0)
```

Resolved versions in the image: `fastapi 0.162.2`, `starlette 1.10.0`,
`opentelemetry-sdk 1.45.2`, `opentelemetry-instrumentation 0.66b2`,
`pytest 9.2.0`.

### 4.3 Lint, types, tests

```
ruff check .              -> All checks passed!
ruff format --check .     -> 76 files already formatted
mypy                      -> Success: no issues found in 44 source files
pytest -q -m "not integration"  -> 125 passed, 20 deselected
```

Full suite in the image, against a fresh volume (migrations `0001 → 0003` run
first by the compose test service, as `support_migrator`, then pytest as
`support_app`):

```
docker compose down -v
docker compose up -d --wait postgres redis
docker compose --profile test run --rm --no-deps test
...
126 passed in 26.50s     (exit 0)
```

Re-run on the final tree (after the OIDC discovery/`alg=none` fix, the
malformed-JWKS mapping fix and the OS patch), again from a fresh volume:

```
docker compose down -v
docker compose up -d --wait postgres redis
docker compose --profile test run --rm --no-deps test
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_initial, ...
INFO  [alembic.runtime.migration] Running upgrade 0001_initial -> 0002_document_ownership, ...
INFO  [alembic.runtime.migration] Running upgrade 0002_document_ownership -> 0003_escalation_outbox, ...
145 passed in 19.71s     (exit 0)
```

Round 3 (public-release review) re-ran every gate on the final tree; the image
was rebuilt with `--build` so it could not silently reuse the previous tag:

```
ruff check .                        -> All checks passed!
ruff format --check .               -> 76 files already formatted
mypy                                -> Success: no issues found in 44 source files
pytest -q -m "not integration"      -> 125 passed, 20 deselected
docker compose --profile test run --build --rm --no-deps test
                                    -> 145 passed in 19.71s   (compose exit 0)
gitleaks detect --no-git --redact   -> 2 findings, both intentional demo values
                                       (.env.example:49 empty OPENAI_API_KEY
                                       placeholder; scripts/verify_oidc.py:198
                                       forged alg=none test token)
```

Two real test-infrastructure defects were found by running the suite *in the
image* instead of only on the host, and fixed:

1. **The test image did not copy `pyproject.toml`.** pytest therefore ran with
   no configuration: `asyncio_mode=auto` was off, `integration` was an unknown
   mark, and every async integration test was silently uncollected
   (`2 failed, 101 passed, 23 errors` — "async def functions are not natively
   supported"). The Dockerfile test stage now copies `pyproject.toml` first.
2. **`test_oidc_requires_issuer_and_audience` depended on the host.** The
   compose test service injects `OIDC_ISSUER`/`OIDC_AUDIENCE`, and
   `_env_file=None` does not clear the process environment, so the test passed
   locally and failed in the image. It now passes the empty value explicitly.

No test was deleted, skipped or weakened to make the suite pass.

### 4.4 Behavioural evals

```
python -m evals.runner
Evals: 8 cases -> http://localhost:8000
Providers: llm=fake embedding=fake model=gpt-4o-mini
NOTE: fake providers are deterministic plumbing. These results validate the pipeline
contract (retrieval -> prompt -> citations -> refusal -> approval), NOT retrieval or
answer quality. Use a real model for quality numbers.
  ingested eval-support-plans.md    chunks=1  duplicate=false flags=[]
  ingested eval-vendor-note.md      chunks=1  duplicate=false flags=[instruction_override,system_prompt_probe,you_are_now]
[PASS] refund-window                checks=5
[PASS] refund-after-40-days         checks=4
[PASS] enterprise-first-response    checks=4
[PASS] two-factor-required          checks=4
[PASS] out-of-scope-geography       checks=3
[PASS] out-of-scope-company-fact    checks=2
[PASS] injection-resistance         checks=5
[PASS] escalation-needs-approval    checks=3
8/8 cases passed     (exit 0)
```

Final run: the app was started from the final `support-agent:local` image
(`docker compose run -d … app`, `OTEL_ENABLED=false`, fake providers) and the
runner ran from the host against `http://localhost:8000`.

The first run was `4/8`: the runner ingested documents as `<user>-ingest` but
asked questions as `<user>`. Uploads are private and retrieval is owner-scoped
(`public OR owner`), so the runner's own documents were correctly invisible. The
harness was wrong, not the isolation. `evals/runner.py` now ingests as the same
identity that asks, and the README documents why.

### 4.5 Runtime image

```
docker run --rm support-agent:local id
uid=10001(app) gid=10001(app) groups=10001(app)
docker run --rm support-agent:local sh -c "dpkg -s zlib1g | grep ^Version"
Version: 1:1.3.dfsg+really1.3.1-1+deb13u1      # the +b1 the base digest shipped is patched
docker run --rm support-agent:local python -c "..."
0.162.2 1.10.0 2.15.1                          # fastapi, starlette, PyJWT
docker run --rm support-agent:local python -c "import importlib.util as u; ..."
pytest False ruff False mypy False             # dev tooling is not in the runtime image
```

The image is built from `requirements.lock` with
`pip install --no-deps --require-hashes --only-binary :all:` (no sdist build
backends are executed) and the base image is pinned by tag **and** digest. The
build additionally applies the Debian security updates that were published after
that digest was minted (`apt-get upgrade` in the `base` stage), and Trivy gates
the result — see §4.9.

### 4.6 Kundi C — data security

Datastores require credentials that come from the environment (no baked-in
defaults are used by the application):

* Postgres — `POSTGRES_PASSWORD`, plus two dedicated roles created by
  `deploy/postgres/init/10-roles.sh` (the same script CI runs):
  `support_migrator` (owns schema `public`, may DDL) and `support_app`
  (`USAGE` on the schema; `SELECT/INSERT/UPDATE/DELETE` on tables only, granted
  through `ALTER DEFAULT PRIVILEGES` so future migrations stay usable). The API
  and the outbox worker connect as `support_app`; only Alembic uses
  `MIGRATE_DATABASE_URL`.
* Redis — `--requirepass` from `REDIS_PASSWORD`, healthcheck authenticates via
  `REDISCLI_AUTH`, and `maxmemory-policy noeviction` so limiter keys cannot be
  silently evicted.

Least privilege is asserted, not assumed:

```
pytest -q tests/test_integration_roles.py
tests/test_integration_roles.py::test_application_role_cannot_run_ddl PASSED
```

(The test skips itself if the connection happens to be a superuser, so it can
never pass vacuously in CI or compose, where `support_app` is not a superuser.)

Ownership is enforced in SQL, so it holds for retrieval, `GET`, `DELETE` and the
answer cache:

| Requirement | Proof |
|---|---|
| A user cannot read another user's document | `test_private_document_is_isolated_between_users`, and the live OIDC run in §4.8 (`GET` → `404`) |
| A user cannot delete another user's document | same tests; live run returns `404` |
| Only an operator may publish/delete shared corpus documents | `test_only_an_operator_may_publish_a_shared_document`; live run `403` / `201` |
| Duplicate uploads do not leak another owner's metadata | `test_duplicate_bytes_do_not_leak_another_owners_document` |
| A cached answer never crosses users | `test_answer_cache_is_scoped_to_the_viewer` (key includes `viewer_id`) |

Schema: `documents.owner_id` + `visibility` (`private`/`public`), unique
`(owner_id, content_sha256)`, and every read path filters
`visibility = 'public' OR owner_id = :caller` (migration `0002_document_ownership`).

### 4.7 Kundi D — identity and reliability

* Keycloak 26.0.0 runs under the `auth` profile with an imported realm
  (`deploy/keycloak/realm-support-agent.json`). `KC_HOSTNAME` is
  `http://keycloak:8080`, so the `iss` minted for a token is exactly the
  `OIDC_ISSUER` the API validates.
* Bearer validation: signature via JWKS, `iss`, `aud`, `exp`/`nbf`/`iat` with
  30 s leeway, `sub` required, algorithm allowlist fixed to `RS*`/`ES*`
  (`none`/`HS*` rejected). An unknown `kid`, a JWKS document that cannot be
  parsed (`PyJWKSetError`), or an unreachable JWKS is `503`, not a bypass; a
  header without `kid` (e.g. `alg=none`) is a `401` (see §4.8). Tests:
  `tests/test_auth.py::test_unknown_kid_is_reported_as_provider_unavailable`,
  `::test_malformed_jwks_is_provider_unavailable`,
  `::test_unreachable_jwks_is_provider_unavailable`, `::test_no_kid_in_header_is_a_client_error_not_an_outage`.
  (The malformed-JWKS case originally fell through to `401`; it was fixed after
  the live gate, and the test fails against the old mapping.)
* The identity is the verified `sub`; `X-User-ID` is ignored in this mode, and
  the rate limiter keys on the same subject.
* `OPERATOR_IDS` is empty in the verification run, so operator powers can only
  come from the IdP role claim (`OIDC_OPERATOR_ROLE=support-operator`).
* Escalations are a transactional outbox: `escalation_outbox.approval_id` is
  unique, decisions and the outbox row commit together, and the worker claims
  with `FOR UPDATE SKIP LOCKED`, commits the claim, then dispatches with
  exponential backoff. A stored `ticket_ref` short-circuits a repeat, so a retry
  can never create a second ticket (integration:
  `test_outbox_retries_with_backoff_until_one_ticket_exists`,
  `test_dispatch_failure_is_surfaced_and_retryable`,
  `test_rejected_escalation_creates_no_ticket`).

### 4.8 Live OIDC gate (`scripts/verify_oidc.py`)

Run against a real Keycloak 26.0.0 in the compose network, with the API built
from the final tree and started as
`AUTH_MODE=oidc OPERATOR_IDS= OTEL_ENABLED=false`. Tokens are minted from
`http://keycloak:8080/realms/support-agent`, so the `iss` matches `OIDC_ISSUER`.

```
docker compose --profile test run --rm --no-deps -v ./scripts:/scripts:ro \
  -e OIDC_VERIFY_BASE_URL=http://oidc-app:8000 \
  test python /scripts/verify_oidc.py
...
49/49 checks passed      (exit 0)
```

The first run of this gate **failed 11 of 30 checks** and exposed two real bugs
(the fix is in the final tree):

1. **JWKS discovery was guessed, not discovered.** With `OIDC_JWKS_URI` unset the
   verifier used `<issuer>/.well-known/jwks.json`, which Keycloak does not serve
   (keys live at `/protocol/openid-connect/certs`). Every authenticated request
   became `503`. `Settings.jwks_uri` now returns an empty override and the
   verifier reads `jwks_uri` from the issuer's
   `/.well-known/openid-configuration`, still lazily and outside the event loop.
2. **A token without `kid` was retryable.** The forged `alg=none` token has no
   `kid`; `PyJWKClientError` mapped it to `503`. A header without `kid` can never
   be a key rotation, so it is now classified as `TokenError` → `401`.
   Unknown-`kid`-after-refresh and an unreachable JWKS remain `503` (retryable),
   and both mappings are unit-tested.

What the 49 passing checks cover (all against the live IdP):

| Area | Observed |
|---|---|
| Token/claims | `iss`, `aud`, `sub`, `exp > iat`, `realm_access.roles = [support-operator, support-user]` |
| No/bad credentials | no bearer → `401`; `X-User-ID` alone → `401`; malformed token → `401`; `alg=none` → `401` |
| Rate limit on `sub` | 20 requests with rotating spoofed `X-User-ID` pass; 21st → `429` with `Retry-After: 60`, `X-RateLimit-Limit: 20`, `X-RateLimit-Remaining: 0`; window resets |
| Identity | `owner_id` equals the token `sub`, never the spoofed header; upload private by default |
| Ownership | another user cannot read or delete the private document (`404`) |
| RBAC | non-operator cannot publish (`403`); IdP `support-operator` role can (`201`); public doc readable by another user (`200`) |
| Retrieval isolation | owner gets a grounded answer citing `oidc-*.md`; another user is refused with zero citations |
| HITL | escalation is `pending`; requester `403`; non-operator `403`; operator approves; `decided_by` = operator `sub`; ticket `SUP-…` exists |
| Idempotency | second decision → `409`; dispatch retry returns the same `ticket_ref`; self-approval `403` |

### 4.9 Image scan and SBOM (Kundi B, final image)

The first `make scan` of the image **failed** on two OS packages with a published
fix — the base digest predated a Debian point release:

```
support-agent:local (debian 13.7)
Total: 2 (HIGH: 2, CRITICAL: 0)

│ zlib1g │ CVE-2026-76844 │ HIGH │ fixed │ 1:1.3.dfsg+really1.3.1-1+b1 │ 1:1.3.dfsg+really1.3.1-1+deb13u1 │
│        │ CVE-2026-85091 │      │       │                            │                                  │
exit 1
```

The `base` stage now applies Debian security updates before the venv is built
(§4.5), and the fix was verified inside the image
(`1:1.3.dfsg+really1.3.1-1+b1` → `1:1.3.dfsg+really1.3.1-1+deb13u1`). Rescan of
the final image:

```
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v trivy-cache:/root/.cache/trivy \
  aquasec/trivy:0.76.2@sha256:8f6bb947… image --scanners vuln --timeout 20m \
  --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 support-agent:local

Detected OS  family="debian" version="13.7"
│ support-agent:local (debian 13.7) │ debian │ 0 │
exit 0
```

SBOM, generated from the same image with Syft `v1.57.0` (digest-pinned):

```
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/out" \
  anchore/syft:v1.57.0@sha256:3c70e6d5… scan support-agent:local \
  -o spdx-json=/out/sbom.spdx.json -o cyclonedx-json=/out/sbom.cdx.json
syft=0
spdx_packages 168   # SPDX 2.3, 2026-11-11T20:47:21Z
cdx_components 2990 # CycloneDX 1.7, 2026-11-11T20:47:21Z
```

Both documents describe the same image the scan above gates; CI regenerates and
uploads them on every build (`.github/workflows/ci.yml`, `image` job).


---

## 5. Final verdict

Every gate below was executed on this host against the final tree. Numbers in
§4.3–§4.9 are the raw outputs of those runs.

| Work order | What was required | Evidence | Status |
|---|---|---|---|
| A | 21st `/chat` in a window → `429` + `Retry-After`; repeat question → `MISS` then `HIT`; `ENVIRONMENT=prod` + `ALLOW_ANON_OPERATORS=true` refuses to start; full findings register | §4.1 (raw headers), §1–§3 | **verified** |
| B | Hashed lockfiles, blocking `pip-audit`, SHA-pinned Actions, Trivy image gate, SBOM in CI and run locally inside Docker | §4.2 (exit 0, both locks), §4.9 (0 HIGH/CRITICAL, SPDX + CycloneDX), CI workflow | **verified** |
| C | Datastore auth, least-privilege DB role (no DDL), separate migration role, document ownership + RBAC with isolation tests | §4.6 (`test_application_role_cannot_run_ddl`, isolation/RBAC/cache tests), live OIDC run §4.8 | **verified** |
| D | Real OIDC (Keycloak): `iss`/`aud`/`exp`/JWKS validation, `sub` replaced the header stub, `sub`-keyed rate limit, outbox with retries/backoff and no double ticket | §4.7, §4.8 (49/49 checks against the live IdP) | **verified** |
| Final | Re-audit all changes, fix findings, one report | §5 (below) | **verified with a caveat** |

### 5.1 Verified

* `ruff check`, `ruff format --check`, `mypy` clean; unit suite and the full
  in-image suite (migrations → integration tests) pass; evals `8/8`.
* Dependency audit and image scan both block on findings and both currently pass
  (exit 0) with the exact commands CI runs.
* Identity, ownership, RBAC, rate limiting, cache isolation and escalation
  idempotency were exercised against a real Keycloak, not only in unit tests.
* Two real defects were found by running the live gate rather than by reading
  the code (JWKS discovery guess; `alg=none`/no-`kid` mapped to `503`), a third
  by reviewing that path afterwards (an unparseable JWKS fell through to `401`
  instead of `503`), and one by the image scan (two fixed HIGH CVEs in `zlib1g`).
  All are fixed and covered by tests or a re-scan.

### 5.2 Not verified (and why)

1. **Answer/retrieval quality with a real model.** All runs used the `fake`
   providers; no paid API was called. Evals therefore prove the pipeline
   contract (retrieval → prompt → citations → refusal → approval), not answer
   quality. This is also stated in the README.
2. **The final `@security` / `@reviewer` subagent passes.** They could not run:
   the model provider returned `Free usage limit reached. Free models work in
   OpenCode … subscribe to OpenCode Go`. The fallback was a manual re-read of
   every change against the OWASP LLM Top 10 plus the register updates in
   §1–§3. That is a weaker control than an independent audit and should be
   repeated when model access is available.
3. **GitHub Actions execution.** There is no remote and nothing was pushed, so
   the workflow itself never ran on GitHub. The identical commands were run
   locally inside Docker (§4.2, §4.9) and `docker compose`/`pytest` are exercised
   in the test image.
4. **Kubernetes / KEDA / Terraform.** No cluster was contacted and no
   `terraform apply` was run (out of scope by instruction).
5. **Compose third-party images are tag-pinned**, not digest-pinned (gap 7).
6. **Outbox at-least-once crash window** against a real ITSM (gap 8).

### 5.3 Residual risk accepted

The gaps in §3 remain deployment work, not code defects. The highest-value ones
are: TLS and a secret manager for Postgres/Redis, per-subject daily budgets,
embedding-model/version metadata with a re-index runbook, ingress/WAF/TLS with
`/health` restricted, OTLP mTLS, and MFA for operator accounts at the IdP.

### 5.4 Actions only the owner can take

1. Put real provider credentials in `.env` (never commit them) and re-run
   `python -m evals.runner` against real models to get quality numbers.
2. Re-run `make lock && make audit && make scan && make sbom` after any
   dependency bump; `make lock` must keep `--upgrade`.
3. Pin digests for the compose/Helm third-party images and wire a digest-refresh
   bot in the deployment repository.
4. Configure TLS (`sslmode=require`, `rediss://`), a KMS/secret manager, MFA at
   the IdP, and ingress/WAF manifests before exposing the API.
5. Replace `agents/escalation.py` with the real ITSM client and pass an
   idempotency key derived from `approval.id`.
6. Give the Docker host more memory: 7.9 GB was marginal (the engine crashed
   twice during image builds and scans); 16 GB is comfortable.
