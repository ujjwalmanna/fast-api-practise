"""Application lifespan: startup and shutdown.

Expensive, process-wide resources (the database engine, the token verifier) are
created once here and stored on `app.state`. Two reasons this is preferable to
module-level globals:

* **Deterministic shutdown.** The engine's connection pool is disposed of on
  exit, so the process does not leave sockets behind.
* **Testability.** Each test app gets its own engine pointing at its own
  database, with no import-time side effects to undo.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from api_framework.auth.principal import Role
from api_framework.auth.verifiers import build_verifier
from api_framework.core.config import Settings
from api_framework.core.logging import get_logger
from api_framework.db.engine import create_engine, create_session_factory, verify_connectivity
from api_framework.domain.users.repository import UserRepository
from api_framework.domain.users.schemas import UserCreate
from api_framework.domain.users.service import UserService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

logger = get_logger(__name__)


async def _bootstrap_admin(app: FastAPI, settings: Settings) -> None:
    """Create the first admin account if configured and absent.

    Guard rails, because a hardcoded admin credential is a common way to ship a
    backdoor:

    * skipped entirely in production (`Settings` refuses to start if the
      password is set there);
    * never overwrites or resets an existing account;
    * logs that it happened, loudly.
    """
    if settings.is_production or not (settings.first_admin_email and settings.first_admin_password):
        return

    factory = app.state.session_factory
    async with factory() as session:
        users = UserRepository(session)
        if await users.get_by_email(settings.first_admin_email):
            return

        service = UserService(users)
        await service.create_user(
            UserCreate(
                email=settings.first_admin_email,
                password=settings.first_admin_password,
                full_name="Bootstrap Admin",
            ),
            roles=frozenset({Role.ADMIN}),
        )
        await session.commit()

    logger.warning(
        "Bootstrap admin created from APP_FIRST_ADMIN_* settings - change the "
        "password and unset these variables before deploying",
        extra={"email": settings.first_admin_email},
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage resources for the lifetime of the application."""
    settings: Settings = app.state.settings

    logger.info("Starting %s", settings.project_name, extra={"environment": settings.env.value})

    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.token_verifier = build_verifier(settings)

    try:
        await verify_connectivity(engine)
        logger.info("Database connection verified")
    except Exception as exc:
        # Log, but start anyway: a database that is slow to come up should not
        # crash-loop the service. The readiness probe reports "degraded" until
        # it recovers, so no traffic is routed here meanwhile.
        logger.error("Database unreachable at startup", exc_info=exc)

    try:
        await _bootstrap_admin(app, settings)
    except Exception as exc:
        # Creating the convenience admin is optional. If the database is not
        # reachable or not yet migrated, log and carry on: the readiness probe
        # will report "degraded" until the dependency recovers. Crashing here
        # would turn a transient database problem into a crash-loop.
        logger.error("Admin bootstrap skipped", exc_info=exc)

    try:
        yield
    finally:
        await engine.dispose()
        logger.info("Shutdown complete; connection pool disposed")
