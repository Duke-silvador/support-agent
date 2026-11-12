"""OIDC bearer-token verification.

Only asymmetric algorithms are accepted and the algorithm allowlist is fixed
here, so a token cannot downgrade to ``none`` or to HMAC-with-the-public-key.

Validation covers signature (via the provider's JWKS), ``iss``, ``aud``,
``exp``/``nbf``/``iat`` (with a small leeway) and requires ``sub``. The JWKS is
fetched lazily and cached by :class:`jwt.PyJWKClient`; the blocking fetch runs in
a worker thread so it never blocks the event loop.

``OIDC_JWKS_URI`` is optional: when it is unset the key set is discovered from
the issuer's ``/.well-known/openid-configuration`` document. That keeps the
default correct for any compliant provider (Keycloak publishes its keys under
``/protocol/openid-connect/certs``, not ``/.well-known/jwks.json``).

Error mapping is deliberate:

* a token with no ``kid`` (including ``alg=none``) is a client error → 401;
* an unknown ``kid`` after a JWKS refresh, an unreachable JWKS, or a key set
  that cannot be parsed → 503, because a key rotation may simply not have
  propagated yet (or the provider is misbehaving) and the caller should retry;
* a bad signature/claim → 401.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKError,
    PyJWKSetError,
    PyJWTError,
)

from support_agent.core.config import Settings

# RS*/ES* only: never HS* (a public key would become a shared secret) and never
# `none`.
ALLOWED_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
)

_REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub")

JWKSResolver = Callable[[str], str]


class TokenError(Exception):
    """The bearer token is missing, malformed, forged or expired."""


class IdentityProviderUnavailable(RuntimeError):
    """The JWKS endpoint could not be reached; the caller should retry later."""


def discover_jwks_uri(issuer: str, *, timeout: float = 10.0) -> str:
    """Read ``jwks_uri`` from the issuer's OpenID discovery document.

    Blocking on purpose: callers run it in a worker thread. Raises
    :class:`IdentityProviderUnavailable` so a provider outage stays a 503.
    """
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise IdentityProviderUnavailable(f"OIDC discovery failed at {url}") from exc
    uri = document.get("jwks_uri") if isinstance(document, dict) else None
    if not isinstance(uri, str) or not uri.startswith(("http://", "https://")):
        raise IdentityProviderUnavailable(f"OIDC discovery at {url} has no usable jwks_uri")
    return uri


class OIDCVerifier:
    """Verifies JWTs for one issuer/audience pair."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str = "",
        leeway_seconds: int = 30,
        jwks_client: Any | None = None,
        algorithms: tuple[str, ...] = ALLOWED_ALGORITHMS,
        jwks_uri_resolver: JWKSResolver = discover_jwks_uri,
        jwks_client_factory: Callable[..., Any] = PyJWKClient,
    ) -> None:
        if not issuer.strip():
            raise ValueError("OIDC issuer is required")
        if not audience.strip():
            raise ValueError("OIDC audience is required")
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.leeway = leeway_seconds
        self.algorithms = algorithms
        self._jwks_uri = jwks_uri.strip()
        self._jwks_client = jwks_client
        self._jwks_uri_resolver = jwks_uri_resolver
        self._jwks_client_factory = jwks_client_factory
        self._jwks_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> OIDCVerifier:
        return cls(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_uri=settings.jwks_uri,
            leeway_seconds=settings.oidc_leeway_seconds,
        )

    def _client(self) -> Any:
        """Build the JWKS client on first use (discovery needs a network call)."""
        if self._jwks_client is not None:
            return self._jwks_client
        with self._jwks_lock:
            if self._jwks_client is None:
                uri = self._jwks_uri or self._jwks_uri_resolver(self.issuer)
                self._jwks_client = self._jwks_client_factory(
                    uri, cache_keys=True, lifespan=300, timeout=10
                )
        return self._jwks_client

    def _signing_key(self, token: str) -> Any:
        # A header without `kid` (for example `alg=none`) can never be a key
        # rotation: treat it as a client error instead of an outage.
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError as exc:
            raise TokenError("malformed token header") from exc
        if not header.get("kid"):
            raise TokenError("token header has no key id")
        return self._client().get_signing_key_from_jwt(token)

    async def verify(self, token: str) -> dict[str, Any]:
        """Return validated claims or raise ``TokenError``/``IdentityProviderUnavailable``."""
        try:
            signing_key = await asyncio.to_thread(self._signing_key, token)
        except (TokenError, IdentityProviderUnavailable):
            raise
        except (PyJWKClientConnectionError, OSError) as exc:
            # The JWKS endpoint itself is unreachable: retryable.
            raise IdentityProviderUnavailable("signing keys unavailable") from exc
        except (PyJWKClientError, PyJWKError, PyJWKSetError) as exc:
            # Unknown `kid` (a rotation that may not have propagated), or a key
            # set that cannot be parsed: the provider is at fault, so the caller
            # should retry rather than be told the token is invalid.
            raise IdentityProviderUnavailable("signing keys unavailable") from exc
        except Exception as exc:  # malformed header must not become a 500
            raise TokenError("malformed token header") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except PyJWTError as exc:
            raise TokenError("invalid token") from exc
        if not isinstance(claims, dict):  # pragma: no cover - jwt.decode returns a dict
            raise TokenError("invalid token payload")
        # Bind the token to this API's client. Without this, identity tokens of
        # the same client (aud == audience) or tokens minted by any other client
        # that merely adds the audience mapper would be accepted.
        authorized_party = claims.get("azp") or claims.get("client_id")
        if (
            isinstance(authorized_party, str)
            and authorized_party.strip()
            and authorized_party.strip() != self.audience
        ):
            raise TokenError("token was issued to a different client")
        token_kind = claims.get("typ") or claims.get("token_use")
        if isinstance(token_kind, str) and token_kind.strip().lower() in {"id", "id_token"}:
            raise TokenError("identity tokens are not accepted")
        return claims


def extract_roles(claims: dict[str, Any]) -> frozenset[str]:
    """Collect realm roles from the Keycloak ``realm_access`` claim.

    Only the realm-role namespace the API's client controls is trusted. Generic
    top-level ``roles``/``groups`` claims are user/IdP-mappable data (group names,
    attribute mappers), so honouring them would let an IdP administrator — or a
    user who can create a group — grant themselves operator powers.
    """
    roles: set[str] = set()
    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        values = realm.get("roles")
        if isinstance(values, list):
            roles.update(str(v) for v in values)
    return frozenset(roles)
