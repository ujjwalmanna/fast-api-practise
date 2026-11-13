"""User and refresh-token ORM models."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api_framework.auth.principal import Role
from api_framework.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from api_framework.db.types import UtcDateTime


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # Unique + indexed: the login path looks users up by email on every attempt.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    # Never "password". The column name should make it obvious that the
    # plaintext is not stored, so a reviewer notices immediately if it is.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))

    # Comma-separated role values. Adequate and portable for a handful of
    # coarse roles; promote to a `user_roles` association table once roles need
    # their own attributes (granted_by, expires_at) or must be queried.
    roles: Mapped[str] = mapped_column(String(255), nullable=False, default=Role.USER.value)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",  # avoid lazy IO in async context
    )

    @property
    def role_set(self) -> frozenset[Role]:
        parsed: set[Role] = set()
        for value in self.roles.split(","):
            if cleaned := value.strip():
                try:
                    parsed.add(Role(cleaned))
                except ValueError:
                    continue
        return frozenset(parsed)

    def set_roles(self, roles: frozenset[Role] | set[Role]) -> None:
        self.roles = ",".join(sorted(role.value for role in roles))

    def __repr__(self) -> str:
        # No email in the repr: reprs land in logs and tracebacks.
        return f"<User id={self.id} active={self.is_active}>"


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A persisted, revocable refresh token.

    Access tokens are stateless and short-lived - verifying one never touches
    the database. Refresh tokens are the opposite: long-lived, so they must be
    revocable, which means server-side state.

    Only a SHA-256 hash of the token is stored. A database leak then does not
    hand the attacker usable sessions. SHA-256 (not Argon2) is correct here: the
    token is 256 bits of entropy from a CSPRNG, so it is not brute-forceable and
    needs no slow hash - and refresh happens often enough that speed matters.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(
        String(36),
        # CASCADE relies on `PRAGMA foreign_keys=ON` - see db/engine.py.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (
        # Supports "revoke every live token for this user" on password change.
        Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),
    )

    def is_usable(self, now: dt.datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now
