"""Composition root for request-scoped dependencies.

Repositories and services are assembled here, in one place, rather than being
constructed inside routes. A route asks for `ItemServiceDep` and receives a
fully wired service; it never knows that a repository or a session exists.

This is what makes the layering real instead of aspirational, and it is the
seam tests use: `app.dependency_overrides[get_item_service] = ...` swaps the
implementation with no patching.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request

from api_framework.core.config import Settings
from api_framework.db.session import SessionDep
from api_framework.domain.items.repository import ItemRepository
from api_framework.domain.items.service import ItemService
from api_framework.domain.users.repository import RefreshTokenRepository, UserRepository
from api_framework.domain.users.service import AuthService, UserService


def get_settings_dep(request: Request) -> Settings:
    """Settings from app state, so tests can inject a different instance."""
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


# --- repositories ---------------------------------------------------------


def get_user_repository(session: SessionDep) -> UserRepository:
    return UserRepository(session)


def get_refresh_token_repository(session: SessionDep) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


def get_item_repository(session: SessionDep) -> ItemRepository:
    return ItemRepository(session)


# --- services -------------------------------------------------------------


def get_user_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    return UserService(users)


def get_auth_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_tokens: Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)],
    settings: SettingsDep,
) -> AuthService:
    return AuthService(users, refresh_tokens, settings)


def get_item_service(
    items: Annotated[ItemRepository, Depends(get_item_repository)],
) -> ItemService:
    return ItemService(items)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
ItemServiceDep = Annotated[ItemService, Depends(get_item_service)]


# --- pagination -----------------------------------------------------------


# Module-level, not a class attribute. Because `from __future__ import
# annotations` makes annotations strings, FastAPI resolves them against module
# globals at startup - a class attribute is not in scope there and fails with
# "is not fully defined".
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class PaginationParams:
    """Shared, validated pagination parameters.

    `limit` is capped at `MAX_PAGE_SIZE`. An uncapped page size is a
    denial-of-service vector: one `?limit=10000000` request can exhaust memory
    serialising the result. The cap is enforced by FastAPI, so it is also
    documented in the OpenAPI schema rather than being an undocumented surprise.
    """

    def __init__(
        self,
        limit: Annotated[
            int, Query(ge=1, le=MAX_PAGE_SIZE, description="Rows per page")
        ] = DEFAULT_PAGE_SIZE,
        offset: Annotated[int, Query(ge=0, description="Rows to skip")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


PaginationDep = Annotated[PaginationParams, Depends()]
