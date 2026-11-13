"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from api_framework.api.deps import AuthServiceDep, UserServiceDep
from api_framework.auth.dependencies import CurrentPrincipal
from api_framework.domain.users.schemas import (
    LoginRequest,
    PrincipalRead,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserRead,
)

router = APIRouter(tags=["Authentication"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new account",
    responses={409: {"description": "Email already registered"}},
)
async def register(payload: UserCreate, users: UserServiceDep) -> UserRead:
    """Create an account with the default USER role.

    Roles are never taken from the request body - self-assignment of ADMIN
    would be a trivial privilege escalation. Elevating a user is an
    admin-only operation.
    """
    user = await users.create_user(payload)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Exchange credentials for tokens (JSON)",
    responses={401: {"description": "Incorrect email or password"}},
)
async def login(payload: LoginRequest, auth: AuthServiceDep) -> TokenPair:
    user = await auth.authenticate(payload.email, payload.password)
    return await auth.issue_token_pair(user)


@router.post(
    "/token",
    response_model=TokenPair,
    summary="Exchange credentials for tokens (OAuth2 form)",
    include_in_schema=True,
)
async def login_oauth2_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    auth: AuthServiceDep,
) -> TokenPair:
    """RFC 6749 password-grant shaped variant of `/login`.

    Exists so the Swagger UI "Authorize" button and generic OAuth2 client
    libraries work unmodified. It takes `username`/`password` as form fields
    rather than JSON; here `username` is the email.
    """
    user = await auth.authenticate(form.username, form.password)
    return await auth.issue_token_pair(user)


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate a refresh token for a new token pair",
    responses={401: {"description": "Refresh token invalid, expired, or already used"}},
)
async def refresh(payload: RefreshRequest, auth: AuthServiceDep) -> TokenPair:
    """Single-use refresh with reuse detection - see `AuthService.refresh`."""
    return await auth.refresh(payload.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a refresh token",
)
async def logout(payload: RefreshRequest, auth: AuthServiceDep) -> None:
    """Idempotent: an unknown or already-revoked token still returns 204."""
    await auth.logout(payload.refresh_token)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke every refresh token for the current user",
)
async def logout_all(principal: CurrentPrincipal, auth: AuthServiceDep) -> None:
    """Sign out of every device. The endpoint to hit after a suspected leak."""
    await auth.logout_everywhere(principal.subject)


@router.get("/me", response_model=UserRead, summary="Current user profile")
async def read_current_user(principal: CurrentPrincipal, users: UserServiceDep) -> UserRead:
    """Resolve the token's subject to the live user record.

    Deliberately re-reads from the database rather than trusting the token's
    claims: a token minted before a role change or deactivation still carries
    the stale values until it expires.
    """
    user = await users.get_user(principal.subject)
    return UserRead.model_validate(user)


@router.get(
    "/me/principal",
    response_model=PrincipalRead,
    summary="Inspect the verified token claims",
)
async def read_principal(principal: CurrentPrincipal) -> PrincipalRead:
    """Debugging aid: shows exactly what the verifier extracted from the token.

    Useful when wiring an external IdP and the roles claim is not where you
    expect it (`APP_OIDC_ROLES_CLAIM`).
    """
    return PrincipalRead(
        subject=principal.subject,
        roles=sorted(principal.roles),
        email=principal.email,
        token_id=principal.token_id,
    )
