"""Health and readiness probes."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from api_framework.app import create_app
from api_framework.core.config import Settings


async def test_liveness_needs_no_auth_and_no_database(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_database_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"


async def test_readiness_returns_503_when_database_is_unreachable(
    settings: Settings,
) -> None:
    """Readiness must fail - not raise - when a dependency is down.

    Points the app at an unwritable path so the engine cannot connect. A probe
    that 500s instead of returning 503 confuses orchestrators, which treat a
    connection error and a 503 differently from an unhandled crash.
    """
    broken = settings.model_copy(
        update={"database_url": "sqlite+aiosqlite:////nonexistent-dir/none.db"}
    )
    app = create_app(broken)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            response = await http_client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unavailable"


async def test_info_exposes_no_secrets(client: AsyncClient) -> None:
    """The info endpoint must not leak configuration."""
    response = await client.get("/api/v1/health/info")
    assert response.status_code == 200

    body = response.json()
    assert body["environment"] == "local"
    assert body["auth_mode"] == "local"

    # Regression guard: a "debug info" endpoint is a classic place for
    # credentials to escape.
    serialised = response.text.lower()
    for forbidden in ("secret", "password", "sqlite", "database_url", "token"):
        assert forbidden not in serialised


async def test_docs_are_served_outside_production(client: AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/openapi.json")).status_code == 200


async def test_docs_are_disabled_in_production(settings: Settings) -> None:
    """Interactive docs advertise the whole API surface, admin routes included."""
    production = settings.model_copy(
        update={
            "env": "production",
            "debug": False,
            "secret_key": "a-production-grade-secret-key-of-sufficient-length",
            "first_admin_email": None,
            "first_admin_password": None,
            "cors_origins": ["https://app.example.com"],
        }
    )
    app = create_app(production)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        assert (await http_client.get("/docs")).status_code == 404
        assert (await http_client.get("/openapi.json")).status_code == 404
