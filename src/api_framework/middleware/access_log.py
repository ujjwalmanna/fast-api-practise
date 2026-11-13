"""Access logging and request timing.

Deliberately written with `BaseHTTPMiddleware` so this project shows *both*
middleware styles side by side:

* `middleware/correlation.py` - pure ASGI. Preferred: no task-group overhead,
  safe with streaming responses and background tasks.
* this file - `BaseHTTPMiddleware`. The ergonomic `dispatch(request, call_next)`
  API, at the cost of an anyio task group per request.

Access logging is a reasonable place for it (one line per request, no body
consumption), but for anything performance-sensitive prefer the pure-ASGI form.

Note what this middleware does *not* do: catch exceptions. Error mapping lives
in `errors/handlers.py`. See the comment at the top of that module for why.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from api_framework.core.logging import get_logger

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.types import ASGIApp

logger = get_logger("api_framework.access")

# Health probes fire every few seconds; logging them buries real traffic.
DEFAULT_EXCLUDED_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit one structured log line per request, with server-side duration."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        excluded_paths: frozenset[str] = DEFAULT_EXCLUDED_PATHS,
    ) -> None:
        super().__init__(app)
        self.excluded_paths = excluded_paths

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in self.excluded_paths:
            return await call_next(request)

        # perf_counter, not time(): monotonic, unaffected by clock adjustments.
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The exception handlers own the response; we only record that the
            # request died here, then re-raise untouched.
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "%s %s -> unhandled exception",
                request.method,
                request.url.path,
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "duration_ms": duration_ms,
                    "client_ip": self._client_ip(request),
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Response-Time-ms"] = str(duration_ms)

        # 5xx is our fault -> WARNING so it surfaces in alerting; 4xx is the
        # caller's -> INFO.
        level = logger.warning if response.status_code >= 500 else logger.info
        level(
            "%s %s -> %d (%.2fms)",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            extra={
                "http_method": request.method,
                "http_path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": self._client_ip(request),
                "user_agent": request.headers.get("user-agent", ""),
            },
        )
        return response

    @staticmethod
    def _client_ip(request: Request) -> str:
        """Best-effort client IP.

        `X-Forwarded-For` is only trustworthy when a proxy you control sets it;
        a direct caller can forge it. Treat it as a hint, never as an
        authorisation input.
        """
        if forwarded := request.headers.get("x-forwarded-for"):
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
