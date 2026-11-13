"""Application exception hierarchy.

## How to add exception handling

Every error this API returns deliberately passes through one of these classes.
The rules:

1. **Raise domain exceptions from domain code.** A service or repository raises
   `NotFoundError`, never `HTTPException`. Business logic must not import
   `fastapi` - that is what keeps it unit-testable and reusable from a CLI, a
   worker, or a test.
2. **Subclass `AppError`.** It carries everything the HTTP layer needs
   (`status_code`, `code`, `title`, `detail`, `extra`) so the handler in
   `handlers.py` needs no per-exception branching.
3. **Never leak internals.** `detail` is client-facing. Put diagnostics in
   `extra` only when they are safe to expose; stack traces go to the log, keyed
   by `request_id`, never into the response.

### Adding a new error type

```python
class QuotaExceededError(AppError):
    status_code = 429
    code = "quota_exceeded"
    title = "Quota exceeded"
```

Raise it anywhere. No handler registration needed - `AppError` is handled once,
generically. Only add a dedicated handler for exceptions you do not own (a
third-party client's error class, for example).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every expected, client-facing error.

    Attributes are class-level defaults so subclasses read as declarations,
    and instances may override any of them.
    """

    status_code: int = 500
    code: str = "internal_error"
    title: str = "Internal server error"
    detail: str | None = None

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        title: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.detail = detail or self.detail or self.title
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        if title is not None:
            self.title = title
        self.extra: dict[str, Any] = extra or {}
        self.headers: dict[str, str] = headers or {}
        super().__init__(self.detail)


# --- 4xx: caller's problem -------------------------------------------------


class BadRequestError(AppError):
    status_code = 400
    code = "bad_request"
    title = "Bad request"


class AuthenticationError(AppError):
    """401 - who are you? Credentials are absent, malformed, or expired."""

    status_code = 401
    code = "unauthenticated"
    title = "Authentication required"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        # RFC 9110: a 401 MUST carry WWW-Authenticate.
        self.headers.setdefault("WWW-Authenticate", "Bearer")


class PermissionDeniedError(AppError):
    """403 - we know who you are; you may not do this."""

    status_code = 403
    code = "permission_denied"
    title = "Permission denied"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class ConflictError(AppError):
    """409 - the request collides with existing state (e.g. duplicate email)."""

    status_code = 409
    code = "conflict"
    title = "Conflict"


class ValidationError(AppError):
    """422 - syntactically valid but semantically wrong.

    FastAPI raises its own `RequestValidationError` for schema failures; this
    is for rules the schema cannot express ("end_date must follow start_date").
    """

    status_code = 422
    code = "validation_error"
    title = "Validation failed"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"


# --- 5xx: our problem ------------------------------------------------------


class InternalError(AppError):
    status_code = 500
    code = "internal_error"
    title = "Internal server error"


class DatabaseError(AppError):
    """Raised by the persistence layer when a DB operation fails.

    Wrapping SQLAlchemy's exceptions here means the API layer never has to
    import SQLAlchemy to handle failures, and driver-level messages (which can
    contain connection strings and SQL) never reach the client.
    """

    status_code = 503
    code = "database_unavailable"
    title = "Database error"


class UpstreamServiceError(AppError):
    """A dependency we call failed. 502 rather than 500: it was not us."""

    status_code = 502
    code = "upstream_error"
    title = "Upstream service error"
