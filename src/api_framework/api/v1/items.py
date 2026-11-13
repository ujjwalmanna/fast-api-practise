"""Item CRUD - the reference router.

Copy this file as the starting point for a new resource. It demonstrates the
conventions the rest of the project assumes:

* thin handlers - parse, delegate to a service, serialise; no business logic;
* authorisation declared per route via `require_roles`;
* explicit `response_model` and `status_code`;
* documented non-2xx responses, so the OpenAPI schema is honest;
* pagination through the shared `PaginationParams`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from api_framework.api.deps import ItemServiceDep, PaginationDep
from api_framework.auth.dependencies import CurrentPrincipal, require_roles
from api_framework.auth.principal import Principal, Role
from api_framework.domain.items.schemas import ItemCreate, ItemRead, ItemUpdate, Page

router = APIRouter(tags=["Items"])

ItemId = Annotated[str, Path(min_length=36, max_length=36, description="Item UUID")]

# Errors every authenticated route can return. Declared once and reused so the
# generated OpenAPI documents them without repetition.
AUTH_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing, malformed, or expired bearer token"},
    403: {"description": "Authenticated but lacking the required role"},
}


@router.post(
    "",
    response_model=ItemRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an item",
    responses={**AUTH_RESPONSES, 422: {"description": "Validation failed"}},
)
async def create_item(
    payload: ItemCreate,
    items: ItemServiceDep,
    principal: Annotated[Principal, Depends(require_roles(Role.USER))],
) -> ItemRead:
    """Create an item owned by the caller.

    `principal` is taken from the `require_roles` dependency rather than
    declared separately, so the authorisation check and the identity used for
    ownership can never disagree.
    """
    item = await items.create(payload, principal=principal)
    return ItemRead.model_validate(item)


@router.get(
    "",
    response_model=Page[ItemRead],
    summary="List items",
    responses=AUTH_RESPONSES,
)
async def list_items(
    items: ItemServiceDep,
    principal: CurrentPrincipal,
    pagination: PaginationDep,
    search: Annotated[str | None, Query(max_length=200, description="Filter by name")] = None,
    mine_only: Annotated[bool, Query(description="Restrict to your own items")] = False,
) -> Page[ItemRead]:
    """Return a page of items visible to the caller.

    Regular users see only their own rows; admins see everything. The scoping
    happens in the service, in the query - never by filtering after the fact.
    """
    rows, total = await items.list_items(
        principal=principal,
        limit=pagination.limit,
        offset=pagination.offset,
        search=search,
        mine_only=mine_only,
    )
    return Page[ItemRead](
        items=[ItemRead.model_validate(row) for row in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/{item_id}",
    response_model=ItemRead,
    summary="Fetch one item",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found, or not yours"}},
)
async def get_item(
    item_id: ItemId,
    items: ItemServiceDep,
    principal: CurrentPrincipal,
) -> ItemRead:
    item = await items.get(item_id, principal=principal)
    return ItemRead.model_validate(item)


@router.patch(
    "/{item_id}",
    response_model=ItemRead,
    summary="Partially update an item",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found, or not yours"}},
)
async def update_item(
    item_id: ItemId,
    payload: ItemUpdate,
    items: ItemServiceDep,
    principal: CurrentPrincipal,
) -> ItemRead:
    """PATCH, not PUT: only the supplied fields change.

    A PUT here would require the client to send every field, and omitting one
    would silently null it.
    """
    item = await items.update(item_id, payload, principal=principal)
    return ItemRead.model_validate(item)


@router.delete(
    "/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an item",
    responses={**AUTH_RESPONSES, 404: {"description": "Not found, or not yours"}},
)
async def delete_item(
    item_id: ItemId,
    items: ItemServiceDep,
    principal: CurrentPrincipal,
) -> None:
    """204 with no body. Returning the deleted object invites clients to
    re-save it."""
    await items.delete(item_id, principal=principal)
