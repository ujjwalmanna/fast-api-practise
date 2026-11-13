"""Async engine and session factory.

The engine is created once per process and holds the connection pool. Creating
one per request would defeat pooling entirely - a classic and expensive mistake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api_framework.core.config import Settings
from api_framework.core.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.pool import ConnectionPoolEntry

logger = get_logger(__name__)


def _configure_sqlite(dbapi_connection: Any, _record: ConnectionPoolEntry) -> None:
    """Apply SQLite pragmas on every new connection.

    SQLite's defaults are tuned for embedded use, not for a web service. Without
    these, two things bite you:

    * `foreign_keys` is **OFF** by default, so `ON DELETE CASCADE` and every FK
      constraint are silently ignored. Your schema looks relational and is not.
    * The default rollback journal locks the whole database on write, so a
      single writer blocks all readers. WAL lets readers run concurrently.

    Pragmas are per-connection, so this must run on each checkout, not once.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # Wait rather than raising "database is locked" the instant a write
        # collides with another connection.
        cursor.execute("PRAGMA busy_timeout=5000")
        # FULL is slow; NORMAL is safe under WAL.
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the configured database."""
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        # Verify a pooled connection before handing it out. Cheap insurance
        # against stale connections after a DB restart or idle timeout.
        "pool_pre_ping": True,
    }

    if not settings.is_sqlite:
        # Pool sizing only applies to real client/server databases.
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)

    engine = create_async_engine(settings.database_url, **kwargs)

    if settings.is_sqlite:
        # Attach to the sync engine underneath: aiosqlite wraps a DBAPI
        # connection, and `connect` is emitted by the sync layer.
        event.listen(engine.sync_engine, "connect", _configure_sqlite)

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory.

    `expire_on_commit=False` is important for an async API: with the default
    `True`, touching any attribute after `commit()` triggers a *synchronous*
    lazy refresh, which raises `MissingGreenlet` under asyncio. Keeping objects
    usable after commit is what lets a route return the model it just saved.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def verify_connectivity(engine: AsyncEngine) -> None:
    """Round-trip a trivial query. Used by the readiness probe and at startup."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
