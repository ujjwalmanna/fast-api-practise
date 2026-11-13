"""Migration tests.

The important one here is `test_migrations_match_the_models`. Without it, the
commonest schema bug in a project like this goes unnoticed: someone edits a
model, the tests pass (because the test schema is built with
`create_all`), and the missing migration is only discovered when a deployment
hits a column that does not exist.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import inspect

from api_framework.db.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(sync_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "src/api_framework/migrations"))
    config.set_main_option("sqlalchemy.url", sync_url)
    return config


@pytest.fixture
def migrated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run `alembic upgrade head` against an empty SQLite file.

    `migrations/env.py` reads the URL from application settings, so the
    environment is pointed at the temporary database and the settings cache
    cleared before Alembic imports it.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("APP_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("APP_SECRET_KEY", "migration-test-secret-key-long-enough-here")

    from api_framework.core.config import get_settings

    get_settings.cache_clear()

    # env.py calls asyncio.run(); ensure no loop is already running in this thread.
    command.upgrade(_alembic_config(f"sqlite+aiosqlite:///{db_path}"), "head")

    get_settings.cache_clear()
    return f"sqlite:///{db_path}"


def test_upgrade_head_creates_every_table(migrated_db: str) -> None:
    engine = create_sync_engine(migrated_db)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert expected <= tables, f"missing: {expected - tables}"


def test_migrations_match_the_models(migrated_db: str) -> None:
    """Fail if the models have drifted from the migrations.

    `compare_metadata` is what `alembic revision --autogenerate` uses to decide
    what to emit. If it finds any difference against a fully migrated database,
    a migration is missing - so this test fails the build rather than the
    deployment.

    If this test fails, run:
        alembic revision --autogenerate -m "describe your change"
    then read the generated file before committing it.
    """
    engine = create_sync_engine(migrated_db)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    # SQLite creates internal bookkeeping tables.
                    "include_object": lambda obj, name, type_, reflected, compare: (
                        not (type_ == "table" and name is not None and name.startswith("sqlite_"))
                    ),
                },
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], "Models and migrations have diverged. Missing migration for:\n" + "\n".join(
        f"  - {entry}" for entry in diff
    )


def test_downgrade_is_reversible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migration you cannot roll back is a migration you cannot deploy safely."""
    db_path = tmp_path / "reversible.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("APP_DATABASE_URL", url)
    monkeypatch.setenv("APP_SECRET_KEY", "migration-test-secret-key-long-enough-here")

    from api_framework.core.config import get_settings

    get_settings.cache_clear()
    config = _alembic_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_sync_engine(f"sqlite:///{db_path}")
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    get_settings.cache_clear()

    # Everything except Alembic's own bookkeeping table should be gone.
    assert remaining - {"alembic_version"} == set()


def test_there_is_exactly_one_head() -> None:
    """Two heads mean a merge is needed and `upgrade head` will fail."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory(str(PROJECT_ROOT / "src/api_framework/migrations"))
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"


def test_sqlite_enforces_foreign_keys() -> None:
    """Directly asserts the pragma that makes CASCADE work.

    SQLite silently ignores foreign keys unless `PRAGMA foreign_keys=ON` is set
    per connection - see `db/engine.py`.
    """
    from sqlalchemy import text

    from api_framework.core.config import Settings
    from api_framework.db.engine import create_engine

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        secret_key="pragma-test-secret-key-long-enough-to-validate",
    )
    engine = create_engine(settings)

    async def check() -> tuple[int, str]:
        async with engine.connect() as conn:
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
        await engine.dispose()
        return int(fk), str(journal)

    foreign_keys, journal_mode = asyncio.run(check())

    assert foreign_keys == 1, "foreign key enforcement is OFF - CASCADE will silently no-op"
    # :memory: databases cannot use WAL, so accept either.
    assert journal_mode.lower() in {"wal", "memory"}
