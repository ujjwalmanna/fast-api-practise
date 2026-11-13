"""Shared test fixtures.

Design goals, in order:

1. **Isolation.** Every test gets its own SQLite file, created from the real
   models and deleted afterwards. No shared state, so tests can run in any
   order and in parallel.
2. **Realism.** The app is built by the real `create_app()` factory and its
   real lifespan runs, so engine construction, verifier selection and the admin
   bootstrap are all exercised rather than stubbed.
3. **No network.** `httpx.ASGITransport` talks to the ASGI app in-process, so
   there is no port to bind and no server to start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api_framework.app import create_app
from api_framework.core.config import AuthMode, Environment, Settings
from api_framework.db.engine import create_engine
from api_framework.db.models import Base

ADMIN_EMAIL = "admin@testcorp.io"
ADMIN_PASSWORD = "AdminPass123!"
USER_EMAIL = "user@testcorp.io"
USER_PASSWORD = "UserPass123!"
OTHER_EMAIL = "other@testcorp.io"
OTHER_PASSWORD = "OtherPass123!"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Per-test settings pointing at a throwaway SQLite file.

    Constructed with explicit keyword arguments, which take precedence over
    both the environment and `.env` in pydantic-settings. That matters: without
    it a developer's local `.env` would leak into the test run and results
    would differ between machines.
    """
    return Settings(
        env=Environment.LOCAL,
        debug=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        # Deterministic but obviously non-production signing key.
        secret_key="test-secret-key-that-is-long-enough-to-pass-validation",
        access_token_ttl_minutes=15,
        refresh_token_ttl_days=7,
        auth_mode=AuthMode.LOCAL,
        log_level="WARNING",  # keep test output readable
        log_json=False,
        cors_origins=["http://testserver"],
        first_admin_email=ADMIN_EMAIL,
        first_admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture
async def _schema(settings: Settings) -> AsyncIterator[None]:
    """Create the schema before the app starts.

    Uses `Base.metadata.create_all` rather than running Alembic: it is far
    faster per test, and `test_migrations.py` separately asserts that the
    migrations and the models agree, so this shortcut cannot hide drift.

    The schema must exist *before* the lifespan runs, because the admin
    bootstrap writes to the users table during startup.
    """
    engine = create_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    yield


@pytest.fixture
async def app(settings: Settings, _schema: None) -> AsyncIterator[FastAPI]:
    """The real application, with its real lifespan running."""
    application = create_app(settings)
    # Starlette exposes the lifespan as a context manager; entering it here
    # runs startup (engine, verifier, admin bootstrap) and guarantees shutdown.
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired directly to the ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client


# --- authentication helpers ----------------------------------------------


async def login(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    """Log in and return the raw token payload."""
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


async def register(
    client: AsyncClient, email: str, password: str, full_name: str | None = None
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_tokens(client: AsyncClient) -> dict[str, str]:
    """Tokens for the bootstrap admin created during startup."""
    return await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture
async def admin_headers(admin_tokens: dict[str, str]) -> dict[str, str]:
    return bearer(admin_tokens["access_token"])


@pytest.fixture
async def user_tokens(client: AsyncClient) -> dict[str, str]:
    """A freshly registered ordinary user (role: USER)."""
    await register(client, USER_EMAIL, USER_PASSWORD, "Test User")
    return await login(client, USER_EMAIL, USER_PASSWORD)


@pytest.fixture
async def user_headers(user_tokens: dict[str, str]) -> dict[str, str]:
    return bearer(user_tokens["access_token"])


@pytest.fixture
async def other_user_headers(client: AsyncClient) -> dict[str, str]:
    """A second ordinary user, for cross-tenant isolation tests."""
    await register(client, OTHER_EMAIL, OTHER_PASSWORD, "Other User")
    tokens = await login(client, OTHER_EMAIL, OTHER_PASSWORD)
    return bearer(tokens["access_token"])


@pytest.fixture
def anyio_backend() -> str:
    """Pin anyio to asyncio; httpx/starlette would otherwise try trio too."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Clear the settings singleton around every test.

    `get_settings` is `lru_cache`d. Without this, a test that constructs the
    app via the cached settings could observe another test's configuration.
    """
    from api_framework.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
