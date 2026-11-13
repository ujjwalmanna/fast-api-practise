"""Item business logic, including ownership authorisation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api_framework.core.logging import get_logger
from api_framework.domain.items.models import Item
from api_framework.domain.items.schemas import ItemCreate, ItemUpdate
from api_framework.errors.exceptions import NotFoundError

if TYPE_CHECKING:
    from api_framework.auth.principal import Principal
    from api_framework.domain.items.repository import ItemRepository

logger = get_logger(__name__)


class ItemService:
    """Item operations.

    Role checks ("is this caller a USER?") happen in the route via
    `require_roles`. Object-level checks ("is this caller allowed to touch
    *this row*?") happen here, because only this layer can load the row and
    compare its owner. Conflating the two is how IDOR vulnerabilities appear:
    the route proves the caller is authenticated and then trusts the id.
    """

    def __init__(self, items: ItemRepository) -> None:
        self._items = items

    async def create(self, payload: ItemCreate, *, principal: Principal) -> Item:
        item = Item(
            name=payload.name,
            description=payload.description,
            price=payload.price,
            # Ownership comes from the verified token, never from the request
            # body. Accepting an `owner_id` field would let any caller create
            # rows belonging to anyone.
            owner_id=principal.subject,
        )
        self._items.add(item)
        await self._items.flush()
        logger.info("Item created", extra={"item_id": item.id, "owner_id": item.owner_id})
        return item

    async def get(self, item_id: str, *, principal: Principal) -> Item:
        item = await self._items.get_by_id(item_id)
        if item is None:
            raise NotFoundError(f"Item {item_id} was not found.")
        self._assert_can_access(item, principal)
        return item

    async def list_items(
        self,
        *,
        principal: Principal,
        limit: int,
        offset: int,
        search: str | None = None,
        mine_only: bool = False,
    ) -> tuple[list[Item], int]:
        """List items visible to the caller.

        Non-admins are scoped to their own rows. The filter is applied in the
        query, not by discarding rows afterwards - filtering in Python breaks
        both the total and the page size.
        """
        owner_filter: str | None = None
        if mine_only or not principal.is_admin:
            owner_filter = principal.subject
        return await self._items.list_items(
            limit=limit, offset=offset, owner_id=owner_filter, search=search
        )

    async def update(self, item_id: str, payload: ItemUpdate, *, principal: Principal) -> Item:
        item = await self.get(item_id, principal=principal)
        changes = payload.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(item, field, value)
        await self._items.flush()
        logger.info("Item updated", extra={"item_id": item.id, "fields": sorted(changes)})
        return item

    async def delete(self, item_id: str, *, principal: Principal) -> None:
        item = await self.get(item_id, principal=principal)
        await self._items.delete(item)
        logger.info("Item deleted", extra={"item_id": item_id})

    @staticmethod
    def _assert_can_access(item: Item, principal: Principal) -> None:
        if item.owner_id != principal.subject and not principal.is_admin:
            # 404, not 403. For a row the caller may not see, confirming it
            # exists is itself a leak - it lets an attacker enumerate ids. This
            # is the opposite choice from `require_roles`, where the resource is
            # not secret and only the operation is.
            raise NotFoundError(f"Item {item.id} was not found.")
