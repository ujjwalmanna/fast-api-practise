"""Application factory.

`create_app()` rather than a module-level `app = FastAPI()` because a factory
lets tests build an isolated instance with injected settings, and makes the
startup order explicit.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from api_framework.api.v1 import api_router
from api_framework.core.config import Settings, get_settings
from api_framework.core.lifespan import lifespan
from api_framework.core.logging import configure_logging, get_logger
from api_framework.errors.handlers import register_exception_handlers
from api_framework.middleware.access_log import AccessLogMiddleware
from api_framework.middleware.correlation import CorrelationIdMiddleware

logger = get_logger(__name__)

DESCRIPTION = """
A production-shaped FastAPI base project.

**Authenticating in this UI:** call `POST /api/v1/auth/login` (or press
*Authorize* and use the OAuth2 form), then paste the `access_token`.

* `POST /api/v1/auth/register` - create an account
* `POST /api/v1/auth/login` - obtain an access + refresh token pair
* `POST /api/v1/auth/refresh` - rotate the refresh token

Errors use RFC 9457 `application/problem+json` and carry a `request_id` that
matches the server log.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the application."""
    settings = settings or get_settings()

    # Before anything else, so startup itself is logged in the right format.
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title=settings.project_name,
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        # Interactive docs are disabled in production: they advertise the full
        # API surface, including admin routes, to anyone who finds the host.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Settings on state before the lifespan runs - it reads them from here.
    app.state.settings = settings

    _register_middleware(app, settings)
    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Install middleware.

    ## Ordering - read before changing

    `add_middleware` *prepends*: the last one added is the outermost layer and
    therefore runs first on the way in and last on the way out. The registration
    below is deliberately in reverse of the desired execution order.

    Desired inbound order:

        CorrelationIdMiddleware   <- assigns request_id (outermost)
          AccessLogMiddleware     <- logs, with request_id available
            CORSMiddleware        <- may short-circuit OPTIONS preflight
              router              <- handler

    Correlation must be outermost so that every later log line - including the
    access log and anything an exception handler writes - carries the id.
    """
    # Added first => innermost of the three.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            # Explicit origins, never "*", because credentials are allowed.
            # Browsers reject wildcard + credentials, and `Settings` refuses to
            # start with "*" outside local.
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            # Let browsers read the correlation id so a frontend can surface it
            # in a bug report.
            expose_headers=["X-Request-ID", "X-Response-Time-ms"],
            max_age=600,
        )

    app.add_middleware(AccessLogMiddleware)

    # Added last => outermost. Must stay last.
    app.add_middleware(CorrelationIdMiddleware)


# ASGI entrypoint for `uvicorn api_framework.app:app`.
app = create_app()
