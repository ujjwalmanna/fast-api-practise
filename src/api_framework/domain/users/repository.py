"""User persistence.

## The repository pattern, and why it is here

A repository is the only place that knows how data is stored. Services call it
and never build queries. That buys three things:

* **Testable services.** Swap the repository for a fake and business logic is
  testable with no database.
* **One place for query changes.** Adding an index hint or changing eager-load
  strategy touches one file.
* **No SQLAlchemy above this layer.** Routes and services import no ORM types,
  so the storage engine is replaceable.

Repositories deliberately do **not** commit. The request-scoped session in
`db/session.py` owns the transaction boundary - see the note there.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from api_framework.db.base import utc_now
from api_framework.domain.users.models import RefreshToken, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Look up by email, case-insensitively.

        Emails are stored lowercased by `create`, so a plain equality match on
        the normalised value uses the unique index. Normalising the *input*
        rather than wrapping the column in `lower()` is what keeps the index
        usable - `WHERE lower(email) = ...` would force a scan.
        """
        stmt = select(User).where(User.email == email.strip().lower())
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_users(self, *, limit: int, offset: int) -> tuple[list[User], int]:
        """Return one page of users plus the unfiltered total.

        The count is a separate query rather than a window function so it stays
        portable across SQLite and PostgreSQL.
        """
        total = (await self._session.execute(select(func.count()).select_from(User))).scalar_one()
        stmt = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return rows, int(total)

    def add(self, user: User) -> User:
        """Stage an insert.

        `flush` (not commit) sends the INSERT so server-side defaults and
        constraint violations surface now, while the transaction is still open
        and the caller can react.
        """
        self._session.add(user)
        return user

    async def flush(self) -> None:
        await self._session.flush()

    async def delete(self, user: User) -> None:
        await self._session.delete(user)

    async def touch_last_login(self, user: User) -> None:
        user.last_login_at = utc_now()
        await self._session.flush()


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, token: RefreshToken) -> RefreshToken:
        self._session.add(token)
        return token

    async def revoke(self, token: RefreshToken, *, at: dt.datetime | None = None) -> None:
        token.revoked_at = at or utc_now()
        await self._session.flush()

    async def commit(self) -> None:
        """Commit immediately.

        The one sanctioned exception to "repositories do not commit". Needed
        when a side effect must survive a request that is about to fail: the
        request-scoped session rolls back on *any* exception, so a revocation
        performed just before raising would otherwise be undone. See
        `AuthService.refresh`.
        """
        await self._session.commit()

    async def revoke_all_for_user(self, user_id: str) -> int:
        """Revoke every live token for a user.

        Called on password change and on refresh-token reuse detection. Returns
        the number revoked so the caller can log the blast radius.
        """
        stmt = select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        tokens = list((await self._session.execute(stmt)).scalars().all())
        now = utc_now()
        for token in tokens:
            token.revoked_at = now
        await self._session.flush()
        return len(tokens)

    async def purge_expired(self) -> int:
        """Delete tokens that expired long enough ago to be uninteresting.

        Revoked/expired rows are kept briefly so reuse detection can still see
        them, then removed. Wire this to a scheduled job; unbounded growth of
        this table is a slow leak.
        """
        cutoff = utc_now() - dt.timedelta(days=30)
        stmt = select(RefreshToken).where(RefreshToken.expires_at < cutoff)
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            await self._session.delete(row)
        await self._session.flush()
        return len(rows)
