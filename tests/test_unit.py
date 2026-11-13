"""Unit tests: no HTTP, no database.

These cover the pieces where a subtle bug is both easy to introduce and
invisible in an integration test - password hashing, token verification,
role mapping and configuration safety.
"""

from __future__ import annotations

import datetime as dt

import jwt
import pytest

from api_framework.auth.passwords import hash_password, needs_rehash, verify_password
from api_framework.auth.principal import Principal, Role
from api_framework.auth.verifiers import (
    LocalJwtVerifier,
    _coerce_roles,
    _extract_claim,
    build_verifier,
)
from api_framework.core.config import INSECURE_SECRET_KEY, AuthMode, Environment, Settings
from api_framework.errors.exceptions import AuthenticationError

# --- password hashing -----------------------------------------------------


def test_hash_is_verifiable_and_salted() -> None:
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first.startswith("$argon2id$")
    # Distinct salts, so identical passwords must not produce identical hashes.
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert verify_password("correct horse battery staple", second)


def test_wrong_password_returns_false_rather_than_raising() -> None:
    hashed = hash_password("right")
    assert verify_password("wrong", hashed) is False


def test_a_corrupt_hash_denies_access_instead_of_crashing() -> None:
    """A damaged row must fail closed, not 500."""
    assert verify_password("anything", "not-a-valid-argon2-hash") is False
    assert needs_rehash("not-a-valid-argon2-hash") is True


def test_an_oversized_password_is_rejected() -> None:
    """Hashing an unbounded input is a cheap denial of service."""
    with pytest.raises(ValueError, match="exceeds"):
        hash_password("x" * 2000)


def test_current_parameters_do_not_need_a_rehash() -> None:
    assert needs_rehash(hash_password("fresh")) is False


# --- role coercion --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, frozenset()),
        ([], frozenset()),
        ("admin", frozenset({Role.ADMIN})),
        (["user", "admin"], frozenset({Role.USER, Role.ADMIN})),
        ([" Admin ", "USER"], frozenset({Role.ADMIN, Role.USER})),  # trimmed, lowered
        (["user", "role-from-another-app"], frozenset({Role.USER})),  # unknown dropped
        ([123, "user"], frozenset({Role.USER})),  # non-strings ignored
    ],
)
def test_roles_are_coerced_leniently(raw: object, expected: frozenset[Role]) -> None:
    """An IdP will send roles belonging to other apps; dropping them is safe.

    Failing the whole request because of an unrecognised role would be hostile,
    and an unmapped role grants nothing.
    """
    assert _coerce_roles(raw) == expected


@pytest.mark.parametrize(
    ("claims", "path", "expected"),
    [
        ({"roles": ["a"]}, "roles", ["a"]),
        ({"realm_access": {"roles": ["a"]}}, "realm_access.roles", ["a"]),  # Keycloak
        ({"realm_access": {}}, "realm_access.roles", None),
        ({}, "missing.deep.path", None),
        ({"a": "not-a-dict"}, "a.b", None),
    ],
)
def test_dotted_claim_paths_resolve(claims: dict[str, object], path: str, expected: object) -> None:
    assert _extract_claim(claims, path) == expected


# --- Principal ------------------------------------------------------------


def test_principal_is_immutable() -> None:
    """Nothing downstream may escalate its own privileges mid-request."""
    principal = Principal(subject="u1", roles=frozenset({Role.USER}))
    with pytest.raises((AttributeError, TypeError)):
        principal.roles = frozenset({Role.ADMIN})  # type: ignore[misc]


def test_has_role_does_not_grant_admin_implicitly() -> None:
    """Implicit superuser bypasses hide authorisation bugs.

    ADMIN is granted explicitly by `require_roles`, not silently by `has_role`.
    """
    admin = Principal(subject="u1", roles=frozenset({Role.ADMIN}))
    assert admin.is_admin is True
    assert admin.has_role(Role.USER) is False
    assert admin.has_role(Role.ADMIN) is True


# --- local JWT verifier ---------------------------------------------------


def _settings(**overrides: object) -> Settings:
    base = {
        "env": Environment.LOCAL,
        "secret_key": "unit-test-secret-key-long-enough-for-validation",
        "first_admin_email": None,
        "first_admin_password": None,
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


def _token(settings: Settings, **claim_overrides: object) -> str:
    now = dt.datetime.now(dt.UTC)
    claims: dict[str, object] = {
        "sub": "user-123",
        "email": "u@testcorp.io",
        "roles": ["user"],
        "type": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "exp": now + dt.timedelta(minutes=5),
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, settings.secret_key, algorithm="HS256")


async def test_a_valid_token_yields_a_principal() -> None:
    settings = _settings()
    principal = await LocalJwtVerifier(settings).verify(_token(settings))

    assert principal.subject == "user-123"
    assert principal.roles == frozenset({Role.USER})
    assert principal.email == "u@testcorp.io"


@pytest.mark.parametrize(
    ("overrides", "description"),
    [
        ({"type": "refresh"}, "a refresh token must not work as an access token"),
        ({"aud": "some-other-service"}, "audience must be verified"),
        ({"iss": "some-other-issuer"}, "issuer must be verified"),
    ],
)
async def test_invalid_claims_are_rejected(overrides: dict[str, object], description: str) -> None:
    settings = _settings()
    with pytest.raises(AuthenticationError):
        await LocalJwtVerifier(settings).verify(_token(settings, **overrides))
    assert description


async def test_a_token_missing_required_claims_is_rejected() -> None:
    """`options={"require": [...]}` must be enforced."""
    settings = _settings()
    token = jwt.encode(
        {"aud": settings.jwt_audience, "iss": settings.jwt_issuer, "type": "access"},
        settings.secret_key,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        await LocalJwtVerifier(settings).verify(token)


async def test_an_unsigned_token_is_rejected() -> None:
    """The `alg: none` attack. `algorithms=` is pinned, so this must fail."""
    settings = _settings()
    unsigned = jwt.encode(
        {
            "sub": "attacker",
            "type": "access",
            "aud": settings.jwt_audience,
            "iss": settings.jwt_issuer,
            "iat": dt.datetime.now(dt.UTC),
            "exp": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        await LocalJwtVerifier(settings).verify(unsigned)


def test_verifier_selection_follows_auth_mode() -> None:
    assert isinstance(build_verifier(_settings()), LocalJwtVerifier)

    external = _settings(
        auth_mode=AuthMode.EXTERNAL,
        oidc_jwks_url="https://idp.example.com/.well-known/jwks.json",
        oidc_audience="my-api",
    )
    verifier = build_verifier(external)
    assert type(verifier).__name__ == "JwksVerifier"


def test_external_mode_requires_a_jwks_url() -> None:
    with pytest.raises(ValueError, match="JWKS"):
        build_verifier(_settings(auth_mode=AuthMode.EXTERNAL, oidc_jwks_url=None))


# --- configuration safety -------------------------------------------------


def test_local_tolerates_the_insecure_defaults() -> None:
    """Zero-friction local development is the whole point of the defaults."""
    settings = Settings(env=Environment.LOCAL, secret_key=INSECURE_SECRET_KEY)
    assert settings.secret_key == INSECURE_SECRET_KEY


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"secret_key": INSECURE_SECRET_KEY}, "APP_SECRET_KEY"),
        ({"secret_key": "too-short"}, "at least 32"),
        ({"debug": True}, "APP_DEBUG"),
        ({"cors_origins": ["*"]}, "CORS"),
        ({"auth_mode": AuthMode.EXTERNAL}, "JWKS"),
    ],
)
def test_unsafe_deployed_configuration_refuses_to_boot(
    overrides: dict[str, object], expected_message: str
) -> None:
    """A service that starts with a forgeable signing key is worse than one
    that does not start, because nothing tells you."""
    good = {
        "env": Environment.STAGING,
        "secret_key": "a-perfectly-long-and-random-staging-secret-key",
        "debug": False,
        "cors_origins": ["https://app.example.com"],
    }
    with pytest.raises(ValueError, match=expected_message):
        Settings(**{**good, **overrides})  # type: ignore[arg-type]


def test_production_forbids_a_bootstrap_admin_password() -> None:
    with pytest.raises(ValueError, match="FIRST_ADMIN_PASSWORD"):
        Settings(
            env=Environment.PRODUCTION,
            secret_key="a-perfectly-long-and-random-production-secret",
            debug=False,
            cors_origins=["https://app.example.com"],
            first_admin_password="AnyPassword123!",
        )


def test_production_forbids_request_body_logging() -> None:
    """Request bodies routinely contain PII and credentials."""
    with pytest.raises(ValueError, match="LOG_REQUEST_BODY"):
        Settings(
            env=Environment.PRODUCTION,
            secret_key="a-perfectly-long-and-random-production-secret",
            debug=False,
            cors_origins=["https://app.example.com"],
            first_admin_password=None,
            log_request_body=True,
        )
