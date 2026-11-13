"""Exception handlers producing RFC 9457 Problem Details responses.

## Why handlers and not middleware

The reference implementations this project draws on caught exceptions inside a
`BaseHTTPMiddleware.dispatch`. That works, but FastAPI/Starlette give you a
purpose-built mechanism - `app.add_exception_handler` - and it is the better
tool here:

* `BaseHTTPMiddleware` wraps the response in an anyio task group. Catching
  exceptions there interferes with streaming responses and background tasks,
  and it cannot see `HTTPException`s raised by Starlette's own routing layer
  (404 on an unknown path never reaches your middleware).
* Handlers receive the `Request`, so they can attach the correlation id.
* Handlers compose: one generic `AppError` handler covers the whole hierarchy,
  and specific handlers are added only where needed.

Middleware is still the right tool for cross-cutting concerns that are not
error mapping - see `middleware/` for correlation ids and access logging.

## The response envelope

All errors share one shape, `application/problem+json` per RFC 9457:

```json
{
  "type":     "/problems/not_found",
  "title":    "Resource not found",
  "status":   404,
  "detail":   "Item 42 does not exist.",
  "instance": "/api/v1/items/42",
  "code":     "not_found",
  "request_id": "0d5f...",
  "errors":   [ ... ]
}
```

A single predictable envelope means clients write one error path, and
`request_id` lets a user paste an id into a support ticket that maps straight
to the server-side log line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api_framework.core.logging import get_logger, get_request_id
from api_framework.errors.exceptions import AppError

if TYPE_CHECKING:
    from pydantic import ValidationError as PydanticValidationError

logger = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


def _resolve_request_id(request: Request) -> str:
    """Recover the correlation id, preferring the ASGI scope.

    The scope copy is authoritative because the handler for `Exception` runs
    inside Starlette's ServerErrorMiddleware, which is *outside* our
    CorrelationIdMiddleware - so the ContextVar has already been reset by then.
    Falling back to the ContextVar keeps this working if the middleware is ever
    removed or reordered.
    """
    scope_state = request.scope.get("state") or {}
    return scope_state.get("request_id") or get_request_id()


def _problem(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"/problems/{code}",
        "title": title,
        "status": status_code,
        "detail": detail or title,
        "instance": request.url.path,
        "code": code,
    }
    if request_id := _resolve_request_id(request):
        body["request_id"] = request_id
    if errors:
        body["errors"] = errors
    if extra:
        body.update(extra)

    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


# --- Individual handlers ---------------------------------------------------


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    """Handle the whole `AppError` hierarchy in one place.

    Client errors (4xx) are expected: log at INFO without a traceback, or the
    logs fill with noise from ordinary 404s. Server errors (5xx) are bugs: log
    at ERROR with the traceback.
    """
    assert isinstance(exc, AppError)  # noqa: S101 - registered for this type only

    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.error("Request failed: %s", exc.detail, exc_info=exc, extra={"error_code": exc.code})
    else:
        logger.info(
            "Request rejected: %s",
            exc.detail,
            extra={"error_code": exc.code, "status_code": exc.status_code},
        )

    return _problem(
        request,
        status_code=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        extra=exc.extra or None,
        headers=exc.headers or None,
    )


async def handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Map Starlette/FastAPI `HTTPException` into the same envelope.

    Needed because Starlette raises these itself (404 for an unmatched route,
    405 for a bad method) and third-party dependencies still raise them. Without
    this, an API would return two different error shapes.
    """
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101

    code = {
        status.HTTP_401_UNAUTHORIZED: "unauthenticated",
        status.HTTP_403_FORBIDDEN: "permission_denied",
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
        status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    }.get(exc.status_code, f"http_{exc.status_code}")

    detail = exc.detail if isinstance(exc.detail, str) else None
    return _problem(
        request,
        status_code=exc.status_code,
        code=code,
        title=code.replace("_", " ").capitalize(),
        detail=detail,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def handle_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Flatten FastAPI's request-validation errors into `errors[]`.

    FastAPI's default 422 body nests under `detail` with a different shape from
    every other error this API returns. Normalising it means one client-side
    error path.
    """
    assert isinstance(exc, RequestValidationError)  # noqa: S101

    errors = [
        {
            # Drop the leading "body"/"query" segment: the field path is what
            # a client can act on.
            "field": ".".join(str(part) for part in err["loc"][1:]) or str(err["loc"][0]),
            "message": err["msg"],
            "type": err["type"],
        }
        for err in exc.errors()
    ]
    return _problem(
        request,
        # The literal, not `status.HTTP_422_*`: Starlette renamed the constant
        # (UNPROCESSABLE_ENTITY -> UNPROCESSABLE_CONTENT) and the old name now
        # emits a DeprecationWarning, while the new one does not exist on older
        # versions. The number is stable across both.
        status_code=422,
        code="validation_error",
        title="Validation failed",
        detail="One or more fields are invalid.",
        errors=errors,
    )


async def handle_pydantic_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """A model validated *inside* the app failed - that is a server-side bug."""
    pydantic_exc: PydanticValidationError = exc  # type: ignore[assignment]
    logger.error("Internal model validation failed", exc_info=pydantic_exc)
    return _problem(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        title="Internal server error",
        detail="An unexpected error occurred.",
    )


async def handle_database_error(request: Request, exc: Exception) -> JSONResponse:
    """Catch SQLAlchemy errors that escaped the repository layer.

    Driver messages can embed SQL and connection details, so the client gets a
    generic 503 while the real error goes to the log.
    """
    logger.error("Unhandled database error", exc_info=exc)
    return _problem(
        request,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="database_unavailable",
        title="Database error",
        detail="The service is temporarily unable to process this request.",
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler: an unanticipated exception is always a bug.

    Returns a deliberately opaque message. `request_id` is the bridge between
    what the user sees and the traceback in the log.
    """
    logger.error("Unhandled exception", exc_info=exc)
    return _problem(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        title="Internal server error",
        detail="An unexpected error occurred. Quote the request_id when reporting this.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler onto the app. Called once by the app factory.

    Order does not matter: Starlette resolves handlers by exception class,
    walking the MRO and preferring the most specific registration.
    """
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)
    app.add_exception_handler(RequestValidationError, handle_request_validation_error)
    app.add_exception_handler(SQLAlchemyError, handle_database_error)
    # Registering `Exception` installs Starlette's ServerErrorMiddleware hook,
    # which is what turns an unhandled crash into a JSON response instead of a
    # bare connection reset.
    app.add_exception_handler(Exception, handle_unexpected_error)
