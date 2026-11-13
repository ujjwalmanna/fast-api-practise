"""Authentication: registration, login, token rotation, revocation, RBAC."""

from __future__ import annotations

import jwt
import pytest
from httpx import AsyncClient
from tests.conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    USER_EMAIL,
    USER_PASSWORD,
    bearer,
    login,
    register,
)

# --- registration ---------------------------------------------------------


async def test_register_creates_an_active_user_with_the_user_role(
    client: AsyncClient,
) -> None:
    body = await register(client, "new@testcorp.io", "ValidPass123!", "New Person")

    assert body["email"] == "new@testcorp.io"
    assert body["roles"] == ["user"]
    assert body["is_active"] is True
    assert body["full_name"] == "New Person"
    # The response model must never carry the hash.
    assert "hashed_password" not in body
    assert "password" not in body


async def test_register_rejects_a_duplicate_email(client: AsyncClient) -> None:
    await register(client, "dupe@testcorp.io", "ValidPass123!")
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "dupe@testcorp.io", "password": "ValidPass123!"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


async def test_register_normalises_email_case(client: AsyncClient) -> None:
    """Emails are case-insensitive identifiers, so casing must not create duplicates."""
    await register(client, "Mixed.Case@testcorp.io", "ValidPass123!")
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "mixed.case@testcorp.io", "password": "ValidPass123!"},
    )
    assert response.status_code == 409


async def test_register_cannot_self_assign_a_role(client: AsyncClient) -> None:
    """Privilege escalation guard: an extra `roles` field must be ignored."""
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "sneaky@testcorp.io",
            "password": "ValidPass123!",
            "roles": ["admin"],
        },
    )
    assert response.status_code == 201
    assert response.json()["roles"] == ["user"]


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("Short1!", "under the 12-character minimum"),
        ("alllowercase123", "no uppercase"),
        ("ALLUPPERCASE123", "no lowercase"),
        ("NoDigitsInHere!", "no digit"),
    ],
)
async def test_register_enforces_password_policy(
    client: AsyncClient, password: str, reason: str
) -> None:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"weak-{abs(hash(password))}@testcorp.io", "password": password},
    )
    assert response.status_code == 422, reason
    assert response.json()["code"] == "validation_error"


# --- login ----------------------------------------------------------------


async def test_login_returns_a_usable_token_pair(client: AsyncClient) -> None:
    tokens = await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 15 * 60
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    # Access token is a JWT; refresh token is opaque, so it must NOT be.
    assert tokens["access_token"].count(".") == 2
    assert tokens["refresh_token"].count(".") == 0


async def test_login_with_a_wrong_password_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "WrongPass123!"}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_login_does_not_reveal_whether_an_account_exists(
    client: AsyncClient,
) -> None:
    """Both failure modes must be byte-identical, or this is an enumeration oracle."""
    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@testcorp.io", "password": "WrongPass123!"},
    )
    wrong_password = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "WrongPass123!"}
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["detail"] == wrong_password.json()["detail"]


async def test_oauth2_form_login_works_for_swagger_and_generic_clients(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/token",
        data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


# --- access-token verification -------------------------------------------


async def test_me_returns_the_current_user(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/auth/me", headers=user_headers)
    assert response.status_code == 200
    assert response.json()["email"] == USER_EMAIL


async def test_protected_route_requires_a_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


async def test_a_garbage_token_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me", headers=bearer("not-a-jwt"))
    assert response.status_code == 401


async def test_a_token_signed_with_the_wrong_key_is_rejected(
    client: AsyncClient,
) -> None:
    """Guards against accepting a token this service did not sign."""
    forged = jwt.encode(
        {
            "sub": "attacker",
            "type": "access",
            "roles": ["admin"],
            "iss": "api-framework",
            "aud": "api-framework",
            "iat": 0,
            "exp": 9_999_999_999,
        },
        # Long enough to avoid PyJWT's short-key warning; still the wrong key.
        "the-wrong-signing-key-but-long-enough-to-not-warn",
        algorithm="HS256",
    )
    response = await client.get("/api/v1/auth/me", headers=bearer(forged))
    assert response.status_code == 401


async def test_an_expired_token_is_rejected(client: AsyncClient, settings) -> None:
    expired = jwt.encode(
        {
            "sub": "someone",
            "type": "access",
            "roles": ["user"],
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": 1_600_000_000,
            "exp": 1_600_000_060,  # long past
        },
        settings.secret_key,
        algorithm="HS256",
    )
    response = await client.get("/api/v1/auth/me", headers=bearer(expired))
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


async def test_a_refresh_token_cannot_be_used_as_an_access_token(
    client: AsyncClient, user_tokens: dict[str, str]
) -> None:
    """Otherwise the 14-day refresh lifetime becomes the session length."""
    response = await client.get("/api/v1/auth/me", headers=bearer(user_tokens["refresh_token"]))
    assert response.status_code == 401


async def test_an_access_token_cannot_be_used_to_refresh(
    client: AsyncClient, user_tokens: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user_tokens["access_token"]}
    )
    assert response.status_code == 401


# --- refresh rotation ----------------------------------------------------


async def test_refresh_issues_a_new_pair_and_rotates_the_refresh_token(
    client: AsyncClient, user_tokens: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": user_tokens["refresh_token"]}
    )
    assert response.status_code == 200

    rotated = response.json()
    assert rotated["access_token"]
    # Single-use: the returned refresh token must differ from the one spent.
    assert rotated["refresh_token"] != user_tokens["refresh_token"]

    # And the new one works.
    again = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )
    assert again.status_code == 200


async def test_reusing_a_refresh_token_revokes_the_whole_family(
    client: AsyncClient, user_tokens: dict[str, str]
) -> None:
    """The core of rotation-with-reuse-detection.

    Replaying a spent refresh token means it leaked. We cannot tell attacker
    from victim, so every session for that user is revoked.
    """
    original = user_tokens["refresh_token"]

    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]

    # Replay the spent token.
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
    assert replay.status_code == 401
    assert "already been used" in replay.json()["detail"]

    # The legitimate successor is now dead too - that is the point.
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
    ).status_code == 401

    # Logging in again still works.
    assert (await login(client, USER_EMAIL, USER_PASSWORD))["access_token"]


async def test_refresh_with_an_unknown_token_is_401(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": "never-issued"})
    assert response.status_code == 401


# --- logout ---------------------------------------------------------------


async def test_logout_revokes_only_that_refresh_token(
    client: AsyncClient, user_tokens: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": user_tokens["refresh_token"]}
    )
    assert response.status_code == 204

    assert (
        await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": user_tokens["refresh_token"]}
        )
    ).status_code == 401


async def test_logout_is_idempotent(client: AsyncClient) -> None:
    """Logout must never fail, and must not disclose whether a token existed."""
    for _ in range(2):
        response = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": "does-not-exist"}
        )
        assert response.status_code == 204


async def test_logout_all_revokes_every_session(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    # Two concurrent sessions for the same user.
    session_a = await login(client, USER_EMAIL, USER_PASSWORD)
    session_b = await login(client, USER_EMAIL, USER_PASSWORD)

    response = await client.post("/api/v1/auth/logout-all", headers=user_headers)
    assert response.status_code == 204

    for session in (session_a, session_b):
        assert (
            await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]}
            )
        ).status_code == 401


# --- RBAC -----------------------------------------------------------------


async def test_admin_route_rejects_an_ordinary_user(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/users", headers=user_headers)
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "permission_denied"
    # The envelope tells the client which roles would be needed.
    assert body["required_roles"] == ["admin"]


async def test_admin_route_accepts_the_admin(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/users", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["total"] >= 1


async def test_bootstrap_admin_was_created_with_the_admin_role(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["roles"] == ["admin"]


async def test_principal_endpoint_reflects_verified_claims(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/auth/me/principal", headers=user_headers)
    assert response.status_code == 200

    body = response.json()
    assert body["roles"] == ["user"]
    assert body["email"] == USER_EMAIL
    assert body["subject"]
