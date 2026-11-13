"""Request-scoped database session dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api_framework.core.logging import get_logger
from api_framework.errors.exceptions import DatabaseError

# NOTE: every type used in a dependency or route signature must be imported at
# *runtime*, not under `if TYPE_CHECKING`. `from __future__ import annotations`
# turns annotations into strings, and FastAPI resolves them at startup to build
# the dependency graph; a TYPE_CHECKING-only import leaves an unresolvable
# forward reference and fails with "is not fully defined" when the OpenAPI
# schema is generated. This is why `async_sessionmaker` and `AsyncIterator` are
# plain imports here.

logger = get_logger(__name__)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Pull the session factory off application state.

    Stored on `app.state` by the lifespan handler rather than in a module-level
    global. That keeps the app self-contained, so tests can build an app with a
    different database without monkeypatching imports.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


async def get_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    """Yield a session that is committed on success and rolled back on error.

    This is the *unit of work* boundary: one transaction per request. Services
    therefore never commit - they mutate, and the outcome of the request decides
    whether the work is durable. That makes a multi-step operation atomic by
    default instead of by remembering to wrap it.

    SQLAlchemy errors are translated into `DatabaseError` so that callers - and
    the exception handlers - never need to import SQLAlchemy, and driver
    messages containing SQL or credentials never reach the client.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except SQLAlchemyError as exc:
            await session.rollback()
            logger.error("Database error; transaction rolled back", exc_info=exc)
            raise DatabaseError("A database error occurred.") from exc
        except Exception:
            # Application error (e.g. NotFoundError): roll back, then let the
            # exception handlers map it to a response.
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]
