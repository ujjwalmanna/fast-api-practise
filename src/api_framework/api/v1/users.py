"""Admin-only user management.

Every route here is gated at the *router* level rather than per handler:

    APIRouter(dependencies=[Depends(require_roles(Role.ADMIN, allow_admin=False))])

Router-level dependencies are the safer default for an admin surface. Gating
each handler individually means a new route added later is public until someone
remembers to protect it - a failure that is invisible in review.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from api_framework.api.deps import PaginationDep, UserServiceDep
from api_framework.auth.dependencies import CurrentPrincipal, require_roles
from api_framework.auth.principal import Role
from api_framework.domain.items.schemas import Page
from api_framework.domain.users.schemas import UserRead, UserUpdate

router = APIRouter(
    tags=["Users (admin)"],
    dependencies=[Depends(require_roles(Role.ADMIN, allow_admin=False))],
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Requires the admin role"},
    },
)


@router.get("", response_model=Page[UserRead], summary="List users")
async def list_users(users: UserServiceDep, pagination: PaginationDep) -> Page[UserRead]:
    rows, total = await users.list_users(limit=pagination.limit, offset=pagination.offset)
    return Page[UserRead](
        items=[UserRead.model_validate(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Fetch one user",
    responses={404: {"description": "User not found"}},
)
async def get_user(user_id: str, users: UserServiceDep) -> UserRead:
    return UserRead.model_validate(await users.get_user(user_id))


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="Update a user",
    responses={404: {"description": "User not found"}},
)
async def update_user(user_id: str, payload: UserUpdate, users: UserServiceDep) -> UserRead:
    """Update profile fields or deactivate an account.

    Note that `is_active=False` does not invalidate tokens already issued.
    Deactivation should be paired with `AuthService.logout_everywhere`; the
    access token remains valid until it expires (15 minutes by default).
    """
    return UserRead.model_validate(await users.update_user(user_id, payload))


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user",
    responses={
        403: {"description": "Cannot delete your own account"},
        404: {"description": "User not found"},
    },
)
async def delete_user(
    user_id: str,
    users: UserServiceDep,
    principal: CurrentPrincipal,
) -> None:
    """Delete a user and, by cascade, their items and refresh tokens."""
    await users.delete_user(user_id, acting_principal=principal)
