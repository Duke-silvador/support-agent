"""Typed configuration. Everything comes from the environment — no secrets in code."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["fake", "openai", "anthropic"]
EmbeddingProviderName = Literal["fake", "openai"]
Environment = Literal["local", "test", "prod"]


class Settings(BaseSettings):
    """Runtime configuration.

    ``llm_provider`` and ``embedding_provider`` are deliberately separate:
    Anthropic serves chat completions but no embeddings, so the two knobs must
    be selectable independently (e.g. ``LLM_PROVIDER=anthropic`` +
    ``EMBEDDING_PROVIDER=openai``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "support-agent"
    environment: Environment = "local"
    log_level: str = "INFO"

    # --- datastores --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://support:support@localhost:5433/support"
    # Used by Alembic only. In production this is a separate, DDL-capable role;
    # the app role below has no DDL rights (see deploy/postgres/init/10-roles.sh).
    migrate_database_url: str | None = None
    redis_url: str = "redis://localhost:6380/0"

    # --- identity ----------------------------------------------------------
    # `header` is the local-dev stub (spoofable). `oidc` validates a bearer JWT
    # signature, issuer, audience and expiry against the provider's JWKS.
    auth_mode: Literal["header", "oidc"] = "header"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    # Optional override. When empty, the key set is discovered from the issuer's
    # `/.well-known/openid-configuration` document (correct for Keycloak, which
    # publishes keys under `/protocol/openid-connect/certs`).
    oidc_jwks_uri: str = ""
    oidc_leeway_seconds: int = Field(default=30, ge=0, le=300)
    # Keycloak/IdP realm or client role that grants operator powers (e.g.
    # `support-operator`). An empty value disables role-based operators; the
    # OPERATOR_IDS allowlist of `sub` claims still applies.
    oidc_operator_role: str = ""

    # --- providers ---------------------------------------------------------
    llm_provider: LLMProviderName = "fake"
    embedding_provider: EmbeddingProviderName = "fake"
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = Field(default=1536, ge=64, le=2000)
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    # --- authorization -----------------------------------------------------
    # Comma-separated allowlist of operator ids that may decide escalations and
    # read any approval. Empty denies every decision by default; only an
    # explicit local-dev flag may open it. Prod refuses to boot without it.
    operator_ids: str = ""
    allow_anon_operators: bool = False

    # --- request limits ----------------------------------------------------
    rate_limit_requests: int = Field(default=20, ge=1, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    cache_ttl_seconds: int = Field(default=3600, ge=0, le=86_400)
    max_question_chars: int = Field(default=2000, ge=1, le=20_000)
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    # Hard cap on the number of embeddings one upload can cost: 5 MiB with
    # 2400/300 chunking is ~2,500 chunks, which is a large paid API call.
    max_chunks_per_document: int = Field(default=500, ge=1, le=5000)
    max_json_bytes: int = Field(default=1024 * 1024, ge=1024)
    max_output_tokens: int = Field(default=800, ge=64, le=8192)

    # --- retrieval ---------------------------------------------------------
    chunk_size_chars: int = Field(default=2400, ge=200, le=20_000)
    chunk_overlap_chars: int = Field(default=300, ge=0, le=5000)
    retrieval_top_k: int = Field(default=6, ge=1, le=50)
    retrieval_min_score: float = Field(default=0.15, ge=0.0, le=1.0)

    # --- agent -------------------------------------------------------------
    agent_max_iterations: int = Field(default=4, ge=1, le=10)
    tool_timeout_seconds: float = Field(default=8.0, gt=0, le=60)
    tool_max_retries: int = Field(default=1, ge=0, le=3)

    # --- observability -----------------------------------------------------
    otel_enabled: bool = True
    otel_service_name: str = "support-agent"
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_traces_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    # --- escalation outbox worker -------------------------------------------
    outbox_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    outbox_max_attempts: int = Field(default=5, ge=1, le=20)
    outbox_base_backoff_seconds: float = Field(default=2.0, gt=0, le=300)
    outbox_batch_size: int = Field(default=10, ge=1, le=100)
    # A row claimed by a worker that dies mid-dispatch is reclaimed after this.
    outbox_stale_seconds: int = Field(default=120, ge=30, le=3600)

    @model_validator(mode="after")
    def _check_chunking(self) -> Settings:
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be smaller than CHUNK_SIZE_CHARS")
        return self

    @model_validator(mode="after")
    def _check_provider_credentials(self) -> Settings:
        if self.llm_provider == "openai" and not _present(self.openai_api_key):
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        if self.llm_provider == "anthropic" and not _present(self.anthropic_api_key):
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")
        if self.embedding_provider == "openai" and not _present(self.openai_api_key):
            raise ValueError("OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai")
        return self

    @model_validator(mode="after")
    def _check_operators(self) -> Settings:
        if self.environment == "prod":
            if self.allow_anon_operators:
                raise ValueError("ALLOW_ANON_OPERATORS must be false in prod")
            if not self.operators:
                raise ValueError("OPERATOR_IDS must list at least one operator in prod")
        return self

    @property
    def operators(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.operator_ids.split(",") if part.strip())

    @property
    def jwks_uri(self) -> str:
        """Explicit JWKS URL, or ``""`` to let the verifier use OIDC discovery.

        A hardcoded `/.well-known/jwks.json` default is wrong for real providers
        (Keycloak serves its keys at `/protocol/openid-connect/certs`), so an
        unset override must reach the discovery path instead of a guessed URL.
        """
        return self.oidc_jwks_uri.strip()

    @property
    def migration_database_url(self) -> str:
        return self.migrate_database_url or self.database_url

    @model_validator(mode="after")
    def _check_identity(self) -> Settings:
        if self.auth_mode == "oidc":
            missing = [
                name
                for name, value in (
                    ("OIDC_ISSUER", self.oidc_issuer.strip()),
                    ("OIDC_AUDIENCE", self.oidc_audience.strip()),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"{' and '.join(missing)} required when AUTH_MODE=oidc")
        # Header identity is spoofable by design; it must never be accepted in a
        # production deployment. Runs after the operator checks so those keep
        # producing their specific messages.
        if self.environment == "prod" and self.auth_mode != "oidc":
            raise ValueError("AUTH_MODE must be oidc in prod (X-User-ID is spoofable)")
        return self

    @property
    def fake_stack(self) -> bool:
        """True when no external AI provider is involved (tests/evals)."""
        return self.llm_provider == "fake" and self.embedding_provider == "fake"


def _present(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
