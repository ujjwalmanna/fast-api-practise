"""Pluggable token verification.

The seam that makes this project work both standalone and inside an existing
identity estate:

* `LocalJwtVerifier`  - verifies HS256 tokens this service issued itself.
                        Zero external dependencies; clone and run.
* `JwksVerifier`      - verifies RS256/ES256 tokens issued by an external IdP
                        (Keycloak, Entra ID, Auth0, Okta, ...) against its
                        published JWKS. This service never sees a password.

Both satisfy `TokenVerifier`, are selected by `APP_AUTH_MODE`, and are
constructed once at startup. To add a third (opaque-token introspection, mTLS,
HMAC-signed service tokens), implement the protocol and register it in
`build_verifier` - no route or service changes.

## Security notes that are easy to get wrong

* **Always pin the algorithm.** Passing the token's own `alg` back to the
  decoder is the classic JWT confusion attack: an attacker re-signs an RS256
  token as HS256 using the public key as the HMAC secret. `algorithms=` is
  always an explicit allow-list below.
* **Always verify `aud` and `iss`.** Otherwise a valid token minted for a
  different service is accepted by this one.
* **`verify_signature=False` appears nowhere in this file.** It is only ever
  legitimate for inspecting a token you have already verified.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import jwt
from jwt import PyJWKClient

from api_framework.auth.principal import Principal, Role
from api_framework.core.config import Settings
from api_framework.core.logging import get_logger
from api_framework.errors.exceptions import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


@runtime_checkable
class TokenVerifier(Protocol):
    """Turns a bearer token string into a `Principal`, or raises."""

    async def verify(self, token: str) -> Principal: ...


def _coerce_roles(raw: object) -> frozenset[Role]:
    """Map an arbitrary roles claim onto known roles.

    Unknown role strings are dropped rather than rejected: an IdP will happily
    include roles that belong to other applications, and failing the whole
    request because of one is hostile. Dropping is safe - an unmapped role
    grants nothing.
    """
    if raw is None:
        return frozenset()
    values: Sequence[object] = list(raw) if isinstance(raw, list | tuple | set) else [raw]

    roles: set[Role] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            roles.add(Role(value.strip().lower()))
        except ValueError:
            logger.debug("Ignoring unrecognised role claim: %s", value)
    return frozenset(roles)


def _extract_claim(claims: dict[str, Any], path: str) -> object:
    """Read a possibly dotted claim path, e.g. `realm_access.roles` (Keycloak)."""
    current: object = claims
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


class LocalJwtVerifier:
    """Verify symmetric (HS256) tokens minted by this service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._settings.secret_key,
                algorithms=[self._settings.jwt_algorithm],  # pinned, never from token
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
                options={"require": ["exp", "sub", "iat"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Access token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            # One generic message: distinguishing "bad signature" from "wrong
            # audience" only helps an attacker probe the configuration.
            logger.debug("Token rejected: %s", exc)
            raise AuthenticationError("Invalid access token.") from exc

        # A refresh token must not be usable as an access token, or its much
        # longer lifetime becomes the effective session length.
        if claims.get("type") != ACCESS_TOKEN_TYPE:
            raise AuthenticationError("Token is not an access token.")

        return Principal(
            subject=str(claims["sub"]),
            roles=_coerce_roles(claims.get("roles")),
            email=claims.get("email"),
            token_id=claims.get("jti"),
            raw_claims=claims,
        )


class JwksVerifier:
    """Verify asymmetric tokens from an external IdP via its JWKS endpoint.

    `PyJWKClient` caches signing keys in memory and refetches on cache miss, so
    a key rotation is picked up without a redeploy and normal requests do not
    make a network call.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.oidc_jwks_url:
            raise ValueError("APP_OIDC_JWKS_URL is required when APP_AUTH_MODE=external")
        self._settings = settings
        self._jwk_client = PyJWKClient(
            settings.oidc_jwks_url,
            cache_keys=True,
            lifespan=600,
        )
        self._algorithms = ["RS256", "RS384", "RS512", "ES256", "ES384"]

    async def verify(self, token: str) -> Principal:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,  # asymmetric only - never HS*
                audience=self._settings.oidc_audience,
                issuer=self._settings.oidc_issuer,
                options={"require": ["exp", "sub", "iat"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Access token has expired.") from exc
        except jwt.PyJWKClientError as exc:
            # Cannot reach or parse the JWKS: an availability problem on our
            # side, not a bad credential. Surfaced as 401 so callers retry
            # rather than treating it as a permanent rejection, but logged loudly.
            logger.error("JWKS retrieval failed", exc_info=exc)
            raise AuthenticationError("Unable to verify token at this time.") from exc
        except jwt.InvalidTokenError as exc:
            logger.debug("Token rejected: %s", exc)
            raise AuthenticationError("Invalid access token.") from exc

        return Principal(
            subject=str(claims["sub"]),
            roles=_coerce_roles(_extract_claim(claims, self._settings.oidc_roles_claim)),
            email=claims.get("email") or claims.get("preferred_username"),
            token_id=claims.get("jti"),
            raw_claims=claims,
        )


def build_verifier(settings: Settings) -> TokenVerifier:
    """Select the verifier for the configured auth mode. Called once at startup."""
    if settings.is_external_auth:
        logger.info("Auth mode: external (JWKS at %s)", settings.oidc_jwks_url)
        return JwksVerifier(settings)
    logger.info("Auth mode: local (self-issued %s tokens)", settings.jwt_algorithm)
    return LocalJwtVerifier(settings)
