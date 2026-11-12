"""Caller identity.

Two modes, selected by ``AUTH_MODE``:

* ``header`` (local-dev only) — identity comes from ``X-User-ID``. This is
  spoofable by design; ``Settings`` refuses it when ``ENVIRONMENT=prod``.
* ``oidc`` — a ``Authorization: Bearer <jwt>`` token is verified (signature via
  JWKS, ``iss``, ``aud``, ``exp`` and required ``sub``) and the user id is the
  verified ``sub``. The header is ignored entirely in this mode, so a spoofed
  ``X-User-ID`` cannot influence identity, rate limits or audit.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request, status

from support_agent.core.auth import (
    IdentityProviderUnavailable,
    OIDCVerifier,
    TokenError,
    extract_roles,
)
from support_agent.core.config import Settings, get_settings

_USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")

IdentitySource = Literal["header", "oidc"]


class Identity:
    """Authenticated caller. ``user_id`` is the verified subject."""

    __slots__ = ("roles", "source", "user_id")

    def __init__(
        self,
        user_id: str,
        roles: frozenset[str] = frozenset(),
        source: IdentitySource = "header",
    ) -> None:
        self.user_id = user_id
        self.roles = roles
        self.source = source

    def __repr__(self) -> str:  # never leak claims into logs by accident
        return f"Identity(user_id={self.user_id!r}, source={self.source!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Identity)
            and other.user_id == self.user_id
            and other.roles == self.roles
            and other.source == self.source
        )

    def __hash__(self) -> int:
        return hash((self.user_id, self.roles, self.source))


_verifiers: dict[tuple[str, str, str, int], OIDCVerifier] = {}


def get_verifier(settings: Settings) -> OIDCVerifier:
    key = (
        settings.oidc_issuer,
        settings.oidc_audience,
        settings.jwks_uri,
        settings.oidc_leeway_seconds,
    )
    verifier = _verifiers.get(key)
    if verifier is None:
        verifier = OIDCVerifier.from_settings(settings)
        _verifiers[key] = verifier
    return verifier


def reset_verifiers() -> None:
    """Drop cached verifiers (tests, config reloads, shutdown)."""
    _verifiers.clear()


async def get_identity(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Identity:
    if settings.auth_mode == "oidc":
        return await _identity_from_bearer(request, settings)
    return _identity_from_header(request)


async def _identity_from_bearer(request: Request, settings: Settings) -> Identity:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = await get_verifier(settings).verify(token.strip())
    except IdentityProviderUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="identity provider unavailable; retry shortly",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc

    subject = str(claims.get("sub") or "")
    if not _USER_ID_RE.match(subject):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token subject is missing or malformed",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )
    return Identity(user_id=subject, roles=extract_roles(claims), source="oidc")


def _identity_from_header(request: Request) -> Identity:
    # Read the header from the request directly so the dependency has one shape
    # in both modes.
    header_value = request.headers.get("x-user-id")
    if not header_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-User-ID header (auth stub; set AUTH_MODE=oidc in production).",
        )
    if not _USER_ID_RE.match(header_value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed X-User-ID.",
        )
    return Identity(user_id=header_value, source="header")


async def get_user_id(identity: Annotated[Identity, Depends(get_identity)]) -> str:
    """Backwards-compatible dependency that yields the verified subject."""
    return identity.user_id


# Kept for callers that still inject the header explicitly (tests, scripts).
async def parse_user_header(
    x_user_id: Annotated[str | None, Header(alias="X-User-ID")] = None,
) -> str:
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-User-ID header (auth stub; set AUTH_MODE=oidc in production).",
        )
    if not _USER_ID_RE.match(x_user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed X-User-ID.")
    return x_user_id
