"""User and authentication business logic.

Note what this module does *not* import: `fastapi`. Services raise domain
exceptions and return domain objects; the HTTP layer adapts them. That is what
makes this logic reusable from a CLI, a background worker, or a test.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from typing import TYPE_CHECKING

import jwt

from api_framework.auth.passwords import hash_password, needs_rehash, verify_password
from api_framework.auth.principal import Principal, Role
from api_framework.auth.verifiers import ACCESS_TOKEN_TYPE
from api_framework.core.config import Settings
from api_framework.core.logging import get_logger
from api_framework.db.base import utc_now
from api_framework.domain.users.models import RefreshToken, User
from api_framework.domain.users.schemas import TokenPair, UserCreate, UserUpdate
from api_framework.errors.exceptions import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)

if TYPE_CHECKING:
    from api_framework.domain.users.repository import RefreshTokenRepository, UserRepository

logger = get_logger(__name__)


def _hash_refresh_token(token: str) -> str:
    """SHA-256 of the raw refresh token. See `RefreshToken` for why not Argon2."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserService:
    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def create_user(
        self,
        payload: UserCreate,
        *,
        roles: frozenset[Role] | None = None,
    ) -> User:
        email = payload.email.strip().lower()
        if await self._users.get_by_email(email):
            # 409 rather than a generic 400 so clients can offer "sign in instead".
            # This does disclose that an account exists; for a consumer product
            # where that is sensitive, return 202 and send an email instead.
            raise ConflictError("An account with this email already exists.")

        user = User(
            email=email,
            hashed_password=hash_password(payload.password),
            full_name=payload.full_name,
        )
        user.set_roles(roles or frozenset({Role.USER}))
        self._users.add(user)
        await self._users.flush()
        logger.info("User created", extra={"user_id": user.id})
        return user

    async def get_user(self, user_id: str) -> User:
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} was not found.")
        return user

    async def list_users(self, *, limit: int, offset: int) -> tuple[list[User], int]:
        return await self._users.list_users(limit=limit, offset=offset)

    async def update_user(self, user_id: str, payload: UserUpdate) -> User:
        user = await self.get_user(user_id)
        # exclude_unset: distinguishes "field absent" from "explicitly null".
        # Without it, a PATCH of one field wipes the others.
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        await self._users.flush()
        return user

    async def delete_user(self, user_id: str, *, acting_principal: Principal) -> None:
        if user_id == acting_principal.subject:
            # Self-deletion by an admin is how you end up with zero admins.
            raise PermissionDeniedError("You cannot delete your own account.")
        user = await self.get_user(user_id)
        await self._users.delete(user)
        logger.info("User deleted", extra={"user_id": user_id})


class AuthService:
    """Credential verification and token lifecycle."""

    def __init__(
        self,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        settings: Settings,
    ) -> None:
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._settings = settings

    # --- login ------------------------------------------------------------

    async def authenticate(self, email: str, password: str) -> User:
        """Verify credentials and return the user, or raise 401.

        Two deliberate choices:

        * **One error message for every failure.** "Unknown email" vs "wrong
          password" is a user-enumeration oracle.
        * **Hash even when the user does not exist.** Returning early on an
          unknown email makes that path measurably faster, which is the same
          oracle via timing. `_dummy_verify` keeps the cost comparable.
        """
        if self._settings.is_external_auth:
            raise PermissionDeniedError(
                "Password login is disabled: this service is configured to accept "
                "tokens from an external identity provider."
            )

        user = await self._users.get_by_email(email)
        if user is None:
            self._dummy_verify(password)
            raise AuthenticationError("Incorrect email or password.")

        if not verify_password(password, user.hashed_password):
            logger.info("Failed login", extra={"user_id": user.id})
            raise AuthenticationError("Incorrect email or password.")

        if not user.is_active:
            raise PermissionDeniedError("This account is disabled.")

        # Transparently upgrade the hash if cost parameters have since risen.
        if needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(password)

        await self._users.touch_last_login(user)
        return user

    @staticmethod
    def _dummy_verify(password: str) -> None:
        """Burn comparable CPU on the unknown-user path (timing equalisation)."""
        verify_password(
            password,
            "$argon2id$v=19$m=65536,t=3,p=4$"
            "c29tZXNhbHRzb21lc2FsdA$RdescudvJCsgt3ub+b+dWRWJTmaaJObG",
        )

    # --- token issuance ---------------------------------------------------

    async def issue_token_pair(self, user: User) -> TokenPair:
        access_token = self._create_access_token(user)
        refresh_token = await self._create_refresh_token(user)
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._settings.access_token_ttl_minutes * 60,
        )

    def _create_access_token(self, user: User) -> str:
        now = utc_now()
        claims = {
            "sub": user.id,
            "email": user.email,
            "roles": sorted(role.value for role in user.role_set),
            "type": ACCESS_TOKEN_TYPE,
            "iss": self._settings.jwt_issuer,
            "aud": self._settings.jwt_audience,
            "iat": now,
            "nbf": now,
            "exp": now + dt.timedelta(minutes=self._settings.access_token_ttl_minutes),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._settings.secret_key, algorithm=self._settings.jwt_algorithm)

    async def _create_refresh_token(self, user: User) -> str:
        """Mint an opaque refresh token and persist only its hash.

        Opaque (random) rather than a JWT: a refresh token's value is that it
        can be revoked, and revocation needs a database lookup anyway - so a
        self-describing token buys nothing and leaks claims if it is stolen.
        """
        raw_token = secrets.token_urlsafe(48)
        self._refresh_tokens.add(
            RefreshToken(
                user_id=user.id,
                token_hash=_hash_refresh_token(raw_token),
                expires_at=utc_now() + dt.timedelta(days=self._settings.refresh_token_ttl_days),
            )
        )
        await self._users.flush()
        return raw_token

    # --- refresh ----------------------------------------------------------

    async def refresh(self, raw_refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, rotating the refresh token.

        Implements **rotation with reuse detection**:

        1. The presented token is revoked immediately and a new one issued, so
           each refresh token is single-use.
        2. If an *already revoked* token is presented, it was either replayed by
           an attacker or leaked. We cannot tell which, so we revoke the user's
           entire token family, forcing a fresh login.

        This is the OAuth 2.0 Security BCP recommendation for public clients and
        it turns a stolen refresh token from indefinite access into a detectable
        event with a bounded window.
        """
        token_hash = _hash_refresh_token(raw_refresh_token)
        stored = await self._refresh_tokens.get_by_hash(token_hash)

        if stored is None:
            raise AuthenticationError("Invalid refresh token.")

        now = utc_now()

        if stored.revoked_at is not None:
            revoked_count = await self._refresh_tokens.revoke_all_for_user(stored.user_id)
            # MUST commit before raising.
            #
            # The request-scoped session rolls back on any exception, including
            # this one. Without an explicit commit the revocation is undone the
            # moment we raise, the response still says "401 reuse detected",
            # and every leaked token stays live - a security control that
            # reports success while doing nothing. This is the one place a
            # service commits, and this is why.
            await self._refresh_tokens.commit()
            logger.warning(
                "Refresh token reuse detected; revoked all sessions for user",
                extra={"user_id": stored.user_id, "revoked_count": revoked_count},
            )
            raise AuthenticationError("Refresh token has already been used. Please sign in again.")

        if stored.expires_at <= now:
            raise AuthenticationError("Refresh token has expired.")

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Account is no longer active.")

        await self._refresh_tokens.revoke(stored, at=now)
        return await self.issue_token_pair(user)

    # --- logout -----------------------------------------------------------

    async def logout(self, raw_refresh_token: str) -> None:
        """Revoke a single refresh token.

        Idempotent and silent on an unknown token: logout must never fail, and
        reporting "that token does not exist" is an oracle.

        The matching access token stays valid until it expires - that is the
        accepted trade-off of stateless access tokens, and the reason their TTL
        is 15 minutes. If you need instant revocation, check a deny-list of
        `jti` values on every request and accept the per-request lookup.
        """
        stored = await self._refresh_tokens.get_by_hash(_hash_refresh_token(raw_refresh_token))
        if stored is not None and stored.revoked_at is None:
            await self._refresh_tokens.revoke(stored)

    async def logout_everywhere(self, user_id: str) -> int:
        return await self._refresh_tokens.revoke_all_for_user(user_id)
