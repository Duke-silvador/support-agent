"""OIDC verification and identity resolution.

No IdP is involved: tokens are signed with a throwaway RSA key and the JWKS
lookup is stubbed, so the tests exercise exactly the code that production runs
(signature, iss, aud, exp, sub) without network access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from support_agent.core import auth as auth_module
from support_agent.core import identity as identity_module
from support_agent.core.auth import (
    ALLOWED_ALGORITHMS,
    IdentityProviderUnavailable,
    OIDCVerifier,
    TokenError,
    discover_jwks_uri,
    extract_roles,
)
from support_agent.core.config import Settings, get_settings
from support_agent.core.identity import Identity, get_identity, reset_verifiers

ISSUER = "http://keycloak:8080/realms/support-agent"
AUDIENCE = "support-agent-api"
KID = "test-kid"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_PUBLIC_PEM = _PUBLIC_KEY.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


class StubJWKClient:
    def __init__(self, *, kid: str = KID, unavailable: bool = False) -> None:
        self.kid = kid
        self.unavailable = unavailable

    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        if self.unavailable:
            raise jwt.PyJWKClientError("JWKS endpoint unreachable")
        header = jwt.get_unverified_header(token)
        if header.get("kid") != self.kid:
            raise jwt.PyJWKClientError(f"unknown kid {header.get('kid')!r}")
        return SimpleNamespace(key=_PUBLIC_PEM)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(__import__("time").time())
    claims: dict[str, Any] = {
        "sub": "agent-user",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now - 10,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def _token(private_key: Any = _PRIVATE_KEY, *, kid: str = KID, **overrides: Any) -> str:
    return jwt.encode(
        _claims(**overrides),
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _verifier(client: StubJWKClient | None = None) -> OIDCVerifier:
    return OIDCVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="http://keycloak.test/jwks",
        leeway_seconds=0,
        jwks_client=client or StubJWKClient(),  # type: ignore[arg-type]
    )


def test_only_asymmetric_algorithms_are_accepted() -> None:
    # Never HS* (a public key would become a shared secret), never `none`.
    assert "none" not in ALLOWED_ALGORITHMS
    assert all(alg.startswith(("RS", "ES")) for alg in ALLOWED_ALGORITHMS)


@pytest.mark.asyncio
async def test_valid_token_yields_claims() -> None:
    claims = await _verifier().verify(_token())
    assert claims["sub"] == "agent-user"
    assert claims["aud"] == AUDIENCE


@pytest.mark.asyncio
async def test_expired_token_is_rejected() -> None:
    now = int(__import__("time").time())
    expired = _token(iat=now - 600, exp=now - 300)
    with pytest.raises(TokenError):
        await _verifier().verify(expired)


@pytest.mark.asyncio
async def test_wrong_audience_is_rejected() -> None:
    with pytest.raises(TokenError):
        await _verifier().verify(_token(aud="some-other-api"))


@pytest.mark.asyncio
async def test_token_for_another_client_is_rejected() -> None:
    # Same realm, same audience, but minted for a different OIDC client.
    with pytest.raises(TokenError):
        await _verifier().verify(_token(azp="some-other-client"))


@pytest.mark.asyncio
async def test_identity_token_is_rejected() -> None:
    # An ID token of this client has aud == audience; it must not authenticate.
    with pytest.raises(TokenError):
        await _verifier().verify(_token(typ="ID"))


@pytest.mark.asyncio
async def test_token_for_this_client_is_accepted() -> None:
    claims = await _verifier().verify(_token(azp=AUDIENCE, typ="Bearer"))
    assert claims["azp"] == AUDIENCE


@pytest.mark.asyncio
async def test_wrong_issuer_is_rejected() -> None:
    with pytest.raises(TokenError):
        await _verifier().verify(_token(iss="http://evil.test/realms/support-agent"))


@pytest.mark.asyncio
async def test_unsigned_alg_none_token_is_rejected() -> None:
    unsigned = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": KID})
    with pytest.raises(TokenError):
        await _verifier().verify(unsigned)


@pytest.mark.asyncio
async def test_token_signed_with_another_key_is_rejected() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(TokenError):
        await _verifier().verify(_token(other))


@pytest.mark.asyncio
async def test_missing_required_claim_is_rejected() -> None:
    now = int(__import__("time").time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60},
        _PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": KID},
    )
    with pytest.raises(TokenError):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_unknown_kid_is_reported_as_provider_unavailable() -> None:
    token = _token(kid="rotated-key")
    with pytest.raises(IdentityProviderUnavailable):
        await _verifier().verify(token)


@pytest.mark.asyncio
async def test_unreachable_jwks_is_provider_unavailable() -> None:
    verifier = _verifier(StubJWKClient(unavailable=True))
    with pytest.raises(IdentityProviderUnavailable):
        await verifier.verify(_token())


@pytest.mark.asyncio
async def test_malformed_jwks_is_provider_unavailable() -> None:
    # An unparseable key set is a provider fault, not a bad token: the caller
    # should retry later, so this must be a 503 rather than a 401.
    class BrokenJWKS:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            raise jwt.PyJWKSetError("no usable keys in the JWKS document")

    verifier = OIDCVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="http://keycloak.test/jwks",
        leeway_seconds=0,
        jwks_client=BrokenJWKS(),  # type: ignore[arg-type]
    )
    with pytest.raises(IdentityProviderUnavailable):
        await verifier.verify(_token())


@pytest.mark.asyncio
async def test_no_kid_in_header_is_a_client_error_not_an_outage() -> None:
    # `alg=none` and similarly forged headers carry no `kid`; that can never be
    # a key rotation, so it must be a 401 rather than a retryable 503.
    unsigned = jwt.encode(_claims(), key="", algorithm="none")
    with pytest.raises(TokenError):
        await _verifier().verify(unsigned)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None):
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Stand-in for ``httpx.Client`` used by ``discover_jwks_uri``."""

    response: _FakeResponse = _FakeResponse()
    seen_url: str = ""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        type(self).seen_url = url
        return type(self).response


def test_discovery_returns_the_published_jwks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.response = _FakeResponse(
        {"jwks_uri": "http://keycloak:8080/realms/support-agent/protocol/openid-connect/certs"}
    )
    monkeypatch.setattr(auth_module.httpx, "Client", _FakeClient)

    uri = discover_jwks_uri(ISSUER)

    assert uri.endswith("/protocol/openid-connect/certs")
    # The discovery document lives next to the realm, not at a guessed JWKS path.
    assert _FakeClient.seen_url == ISSUER + "/.well-known/openid-configuration"


def test_discovery_without_jwks_uri_is_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeClient.response = _FakeResponse({"issuer": ISSUER})
    monkeypatch.setattr(auth_module.httpx, "Client", _FakeClient)
    with pytest.raises(IdentityProviderUnavailable):
        discover_jwks_uri(ISSUER)


def test_discovery_http_failure_is_provider_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeClient.response = _FakeResponse(error=httpx.ConnectError("boom"))
    monkeypatch.setattr(auth_module.httpx, "Client", _FakeClient)
    with pytest.raises(IdentityProviderUnavailable):
        discover_jwks_uri(ISSUER)


@pytest.mark.asyncio
async def test_jwks_uri_is_discovered_when_not_configured() -> None:
    seen: dict[str, str] = {}

    def resolver(issuer: str) -> str:
        seen["issuer"] = issuer
        return "http://keycloak.test/realms/support-agent/protocol/openid-connect/certs"

    def factory(uri: str, **kwargs: Any) -> StubJWKClient:
        seen["uri"] = uri
        return StubJWKClient()

    verifier = OIDCVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        leeway_seconds=0,
        jwks_uri_resolver=resolver,
        jwks_client_factory=factory,
    )

    claims = await verifier.verify(_token())

    assert claims["sub"] == "agent-user"
    assert seen["issuer"] == ISSUER
    assert seen["uri"].endswith("/protocol/openid-connect/certs")


@pytest.mark.asyncio
async def test_jwks_connection_error_is_provider_unavailable() -> None:
    class DownClient:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            raise jwt.PyJWKClientConnectionError("connection refused")

    verifier = OIDCVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_uri="http://keycloak.test/jwks",
        leeway_seconds=0,
        jwks_client=DownClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(IdentityProviderUnavailable):
        await verifier.verify(_token())


def test_roles_are_collected_only_from_keycloak_realm_roles() -> None:
    keycloak = {
        "realm_access": {"roles": ["support-user", "support-operator"]},
        "resource_access": {"support-agent-api": {"roles": ["ignored-by-design"]}},
    }
    assert extract_roles(keycloak) == frozenset({"support-user", "support-operator"})
    # Generic, IdP-mappable claims must not be able to grant operator powers.
    assert extract_roles({"roles": ["ops"]}) == frozenset()
    assert extract_roles({"groups": ["team-a"]}) == frozenset()
    assert extract_roles({}) == frozenset()


# ---------------------------------------------------------------------------
# get_identity dependency: bearer parsing, status mapping, header shadowing
# ---------------------------------------------------------------------------


class StubVerifier:
    def __init__(self, *, claims: dict[str, Any] | None = None, error: Exception | None = None):
        self.claims = claims or {}
        self.error = error

    async def verify(self, token: str) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return self.claims


@pytest.fixture
def oidc_settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="oidc",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
    )


@pytest.fixture
def auth_app(oidc_settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(identity: Annotated[Identity, Depends(get_identity)]) -> dict[str, Any]:
        return {
            "user_id": identity.user_id,
            "roles": sorted(identity.roles),
            "source": identity.source,
        }

    app.dependency_overrides[get_settings] = lambda: oidc_settings
    return app


@pytest.fixture
async def auth_client(auth_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_bearer_token_subject_is_the_identity(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_verifiers()
    token = _token()
    monkeypatch.setattr(
        identity_module,
        "get_verifier",
        lambda _settings: StubVerifier(
            claims={"sub": "agent-user", "realm_access": {"roles": ["support-operator"]}}
        ),
    )
    # A spoofed X-User-ID must be ignored in OIDC mode.
    response = await auth_client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {token}", "X-User-ID": "admin"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "user_id": "agent-user",
        "roles": ["support-operator"],
        "source": "oidc",
    }


@pytest.mark.asyncio
async def test_missing_token_is_401(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.get("/whoami", headers={"X-User-ID": "admin"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.asyncio
async def test_invalid_token_is_401(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        identity_module,
        "get_verifier",
        lambda _settings: StubVerifier(error=TokenError("invalid token")),
    )
    response = await auth_client.get(
        "/whoami", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401
    assert "invalid_token" in response.headers.get("www-authenticate", "")


@pytest.mark.asyncio
async def test_unavailable_jwks_is_503(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        identity_module,
        "get_verifier",
        lambda _settings: StubVerifier(error=IdentityProviderUnavailable("down")),
    )
    response = await auth_client.get("/whoami", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_missing_subject_is_401(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        identity_module,
        "get_verifier",
        lambda _settings: StubVerifier(claims={"sub": ""}),
    )
    response = await auth_client.get(
        "/whoami", headers={"Authorization": "Bearer token-without-sub"}
    )
    assert response.status_code == 401


def test_reset_verifiers_drops_the_cache() -> None:
    settings = Settings(
        _env_file=None, auth_mode="oidc", oidc_issuer=ISSUER, oidc_audience=AUDIENCE
    )
    first = identity_module.get_verifier(settings)
    assert identity_module.get_verifier(settings) is first
    reset_verifiers()
    assert identity_module.get_verifier(settings) is not first
    reset_verifiers()
