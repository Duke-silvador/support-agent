from __future__ import annotations

import pytest
from pydantic import ValidationError

from support_agent.api.deps import is_operator
from support_agent.core.config import Settings


def test_fake_stack_is_the_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "fake"
    assert settings.embedding_provider == "fake"
    assert settings.fake_stack is True


def test_openai_llm_without_key_fails_fast() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, llm_provider="openai", openai_api_key=None)


def test_openai_embedding_without_key_fails_fast() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, embedding_provider="openai", openai_api_key=None)


def test_anthropic_llm_is_allowed_with_fake_embeddings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
        embedding_provider="fake",
    )
    assert settings.llm_provider == "anthropic"
    assert settings.embedding_provider == "fake"
    assert settings.fake_stack is False


def test_anthropic_embeddings_are_not_a_thing() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_provider="anthropic")  # type: ignore[arg-type]


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValidationError, match="CHUNK_OVERLAP_CHARS"):
        Settings(_env_file=None, chunk_size_chars=400, chunk_overlap_chars=400)


def test_embedding_dim_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_dim=4096)


def test_prod_requires_an_operator_allowlist() -> None:
    with pytest.raises(ValidationError, match="OPERATOR_IDS"):
        Settings(_env_file=None, environment="prod", operator_ids="")


def test_prod_rejects_anonymous_operators() -> None:
    with pytest.raises(ValidationError, match="ALLOW_ANON_OPERATORS"):
        Settings(
            _env_file=None,
            environment="prod",
            operator_ids="operator-1",
            allow_anon_operators=True,
        )


def test_operator_allowlist_is_parsed_and_trimmed() -> None:
    settings = Settings(_env_file=None, operator_ids=" alice, bob ,, ")
    assert settings.operators == frozenset({"alice", "bob"})


def test_only_allowlisted_operators_may_decide() -> None:
    restricted = Settings(_env_file=None, operator_ids="alice,bob")
    assert is_operator("alice", restricted) is True
    assert is_operator("mallory", restricted) is False

    # An empty allowlist denies by default; only an explicit local-dev flag
    # opens it. The requester is still blocked by the SoD check in the route.
    closed = Settings(_env_file=None, operator_ids="")
    assert is_operator("mallory", closed) is False

    dev = Settings(_env_file=None, operator_ids="", allow_anon_operators=True)
    assert is_operator("mallory", dev) is True


def test_role_based_operator_from_the_idp() -> None:
    settings = Settings(_env_file=None, operator_ids="alice", oidc_operator_role="support-operator")
    assert is_operator("bob", settings, frozenset({"support-operator"})) is True
    assert is_operator("bob", settings, frozenset({"support-user"})) is False
    # Role-based operators can be disabled entirely.
    no_role = Settings(_env_file=None, operator_ids="alice", oidc_operator_role="")
    assert is_operator("bob", no_role, frozenset({"support-operator"})) is False


def test_oidc_requires_issuer_and_audience() -> None:
    # Pass the empty value explicitly: `_env_file=None` still lets the process
    # environment (e.g. the compose test service sets OIDC_ISSUER/AUDIENCE)
    # supply a value, which would make this assertion depend on the host.
    with pytest.raises(ValidationError, match="OIDC_ISSUER"):
        Settings(
            _env_file=None,
            auth_mode="oidc",
            oidc_issuer="",
            oidc_audience="support-agent-api",
        )
    with pytest.raises(ValidationError, match="OIDC_AUDIENCE"):
        Settings(
            _env_file=None,
            auth_mode="oidc",
            oidc_issuer="http://keycloak:8080/realms/support-agent",
            oidc_audience="",
        )


def test_oidc_configuration_is_accepted() -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="http://keycloak:8080/realms/support-agent/",
        oidc_audience="support-agent-api",
        # Explicit: the process env may supply a value in the image/CI.
        oidc_jwks_uri="",
    )
    # No hardcoded JWKS guess: an unset override means "use OIDC discovery".
    assert settings.jwks_uri == ""


def test_explicit_jwks_uri_wins_over_discovery() -> None:
    settings = Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer="https://sso.example.test/realms/support-agent",
        oidc_audience="support-agent-api",
        oidc_jwks_uri="  https://sso.example.test/keys  ",
    )
    assert settings.jwks_uri == "https://sso.example.test/keys"


def test_prod_refuses_the_spoofable_header_identity() -> None:
    with pytest.raises(ValidationError, match="AUTH_MODE must be oidc"):
        Settings(_env_file=None, environment="prod", operator_ids="operator-1", auth_mode="header")


def test_prod_oidc_configuration_is_accepted() -> None:
    settings = Settings(
        _env_file=None,
        environment="prod",
        operator_ids="operator-1",
        auth_mode="oidc",
        oidc_issuer="https://sso.example.test/realms/support-agent",
        oidc_audience="support-agent-api",
    )
    assert settings.environment == "prod"
    assert settings.auth_mode == "oidc"
