"""Alembic environment - async engine, SQLite-aware.

Three things here are not in Alembic's generated template and matter:

1. **The URL comes from application settings**, not `alembic.ini`. One source
   of truth, and no credentials in a committed file.
2. **`render_as_batch=True`.** SQLite cannot `ALTER TABLE ... DROP COLUMN` or
   alter a constraint. Batch mode makes Alembic emulate it by creating a new
   table, copying rows, and swapping. Without this, the *first* migration that
   drops or alters a column fails - typically months into a project. It
   requires named constraints, which `db/base.py` guarantees.
3. **Async engine support.** The app uses `sqlite+aiosqlite` / `asyncpg`, and
   Alembic's migration machinery is synchronous, so migrations run inside
   `connection.run_sync()`.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import the model registry so every table is attached to Base.metadata.
# Without this import autogenerate sees an empty schema and emits migrations
# that drop every table. See db/models.py.
from api_framework.core.config import get_settings
from api_framework.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()

# Escape '%' so ConfigParser interpolation does not choke on a URL-encoded
# password (a '%' in a password is otherwise read as an interpolation marker).
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata

IS_SQLITE = settings.is_sqlite


def include_object(object_, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    """Filter objects out of autogenerate.

    SQLite creates internal `sqlite_*` tables; without this filter autogenerate
    proposes dropping them.
    """
    return not (type_ == "table" and name is not None and name.startswith("sqlite_"))


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    """Shared context configuration for both offline and online modes."""
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        # Detect column type changes (off by default, which silently misses
        # e.g. String(50) -> String(200)).
        compare_type=True,
        # Detect server-default changes.
        compare_server_default=True,
        # See the module docstring - essential for SQLite.
        render_as_batch=IS_SQLITE,
        # Wrap each migration in its own transaction. PostgreSQL supports
        # transactional DDL, so a failed migration rolls back cleanly.
        transaction_per_migration=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting - `alembic upgrade head --sql`.

    Useful when a DBA must review and apply the change set by hand.
    """
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations through its sync bridge."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: a migration is a one-shot process, so pooling adds nothing
        # and can leave the connection open long enough to block other work.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
