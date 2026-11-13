"""The error envelope, correlation ids and middleware behaviour."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from api_framework.errors.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from api_framework.middleware.correlation import REQUEST_ID_HEADER

# --- RFC 9457 envelope ----------------------------------------------------


async def test_error_response_is_rfc9457_problem_json(
    client: AsyncClient, user_headers: dict[str, str]
) -> None:
    response = await client.get(f"/api/v1/items/{'0' * 36}", headers=user_headers)

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    for field in ("type", "title", "status", "detail", "instance", "code", "request_id"):
        assert field in body, f"missing {field}"

    assert body["type"] == "/problems/not_found"
    assert body["status"] == 404
    assert body["code"] == "not_found"
    assert body["instance"] == f"/api/v1/items/{'0' * 36}"


async def test_unknown_route_uses_the_same_envelope(client: AsyncClient) -> None:
    """Starlette raises this 404 itself, before any of our code runs.

    This is precisely what a `BaseHTTPMiddleware`-based error handler cannot
    catch, and why handlers are registered instead.
    """
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "not_found"


async def test_method_not_allowed_uses_the_same_envelope(client: AsyncClient) -> None:
    response = await client.delete("/api/v1/health/live")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


async def test_validation_errors_are_flattened(client: AsyncClient) -> None:
    """FastAPI's native 422 shape is normalised into `errors[]`."""
    response = await client.post("/api/v1/auth/register", json={"email": "not-an-email"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["errors"], list)

    fields = {error["field"] for error in body["errors"]}
    assert "email" in fields  # invalid format
    assert "password" in fields  # missing entirely

    for error in body["errors"]:
        assert set(error) == {"field", "message", "type"}


async def test_the_request_id_in_the_body_matches_the_header(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


# --- unhandled exceptions -------------------------------------------------


async def test_an_unhandled_exception_becomes_an_opaque_500(
    settings,
) -> None:
    """Internal detail must never reach the client, but must reach the log."""
    from api_framework.app import create_app

    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        response = await http_client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    # The secret in the exception message must not be echoed.
    assert "hunter2" not in response.text
    assert "RuntimeError" not in response.text
    # But the caller gets an id to quote in a support ticket.
    assert body["request_id"]


async def test_a_custom_app_error_needs_no_handler_registration(
    settings,
) -> None:
    """Subclassing `AppError` is sufficient - the generic handler maps it."""

    class QuotaExceededError(AppError):
        status_code = 429
        code = "quota_exceeded"
        title = "Quota exceeded"

    from api_framework.app import create_app

    app = create_app(settings)

    @app.get("/quota")
    async def quota() -> None:
        raise QuotaExceededError("Monthly limit of 100 reached.", extra={"limit": 100})

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        response = await http_client.get("/quota")

    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "quota_exceeded"
    assert body["detail"] == "Monthly limit of 100 reached."
    assert body["limit"] == 100  # `extra` is merged into the envelope


# --- middleware -----------------------------------------------------------


async def test_a_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == 32  # uuid4().hex


async def test_an_inbound_request_id_is_honoured(client: AsyncClient) -> None:
    """Lets a trace be followed across service hops."""
    response = await client.get("/api/v1/health/live", headers={REQUEST_ID_HEADER: "trace-abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


async def test_request_ids_are_unique_per_request(client: AsyncClient) -> None:
    seen = {(await client.get("/api/v1/health/live")).headers[REQUEST_ID_HEADER] for _ in range(5)}
    assert len(seen) == 5


@pytest.mark.parametrize(
    "hostile",
    [
        "abc\r\nX-Injected: evil",  # header injection attempt
        "a" * 500,  # over-long
        "with,comma",
    ],
)
async def test_a_hostile_inbound_request_id_is_sanitised(client: AsyncClient, hostile: str) -> None:
    """The value is reflected in a response header, so it must be cleaned."""
    response = await client.get("/api/v1/health/live", headers={REQUEST_ID_HEADER: hostile})

    assert response.status_code == 200
    echoed = response.headers[REQUEST_ID_HEADER]
    assert "\r" not in echoed
    assert "\n" not in echoed
    assert "," not in echoed
    assert len(echoed) <= 128
    assert "x-injected" not in {key.lower() for key in response.headers}


async def test_the_response_time_header_is_present(client: AsyncClient) -> None:
    """Added by the access-log middleware - proves it is in the chain."""
    response = await client.get("/api/v1/auth/me")
    assert float(response.headers["X-Response-Time-ms"]) >= 0


async def test_cors_preflight_is_answered_for_an_allowed_origin(
    client: AsyncClient,
) -> None:
    response = await client.options(
        "/api/v1/items",
        headers={
            "Origin": "http://testserver",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://testserver"
    assert "POST" in response.headers["access-control-allow-methods"]


async def test_cors_exposes_the_correlation_id_to_browser_js(
    client: AsyncClient,
) -> None:
    """`Access-Control-Expose-Headers` appears on the actual response.

    Per the Fetch standard it is not a preflight response header, so it must be
    asserted on a real request - a frontend cannot read `X-Request-ID` without it.
    """
    response = await client.get("/api/v1/health/live", headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    exposed = response.headers["access-control-expose-headers"].lower()
    assert REQUEST_ID_HEADER.lower() in exposed


async def test_cors_rejects_an_unlisted_origin(client: AsyncClient) -> None:
    response = await client.options(
        "/api/v1/items",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers


# --- exception classes ----------------------------------------------------


def test_exception_defaults_are_declarative() -> None:
    assert NotFoundError().status_code == 404
    assert ConflictError().status_code == 409
    assert PermissionDeniedError().status_code == 403
    # `detail` falls back to `title` so a bare raise still produces a message.
    assert NotFoundError().detail == "Resource not found"
    assert NotFoundError("Custom message").detail == "Custom message"


def test_authentication_error_always_sets_www_authenticate() -> None:
    """RFC 9110 requires it on a 401."""
    from api_framework.errors.exceptions import AuthenticationError

    assert AuthenticationError().headers["WWW-Authenticate"] == "Bearer"
