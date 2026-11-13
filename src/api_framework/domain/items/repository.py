"""Item persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from api_framework.domain.items.models import Item

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, item_id: str) -> Item | None:
        return await self._session.get(Item, item_id)

    async def list_items(
        self,
        *,
        limit: int,
        offset: int,
        owner_id: str | None = None,
        search: str | None = None,
    ) -> tuple[list[Item], int]:
        """Return one page of items plus the total matching the same filters.

        Filters are built once and applied to both the page query and the count
        query. Building them twice is how the two drift apart and the reported
        total stops matching the rows.
        """
        filters = []
        if owner_id is not None:
            filters.append(Item.owner_id == owner_id)
        if search:
            # `ilike` is case-insensitive on PostgreSQL; on SQLite LIKE is
            # already case-insensitive for ASCII. Escape the wildcards so a
            # user searching for "50%" does not get a wildcard query.
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(Item.name.ilike(f"%{escaped}%", escape="\\"))

        count_stmt = select(func.count()).select_from(Item).where(*filters)
        total = int((await self._session.execute(count_stmt)).scalar_one())

        page_stmt = (
            select(Item)
            .where(*filters)
            # Tie-break on id: ordering by a non-unique column alone gives a
            # non-deterministic order, so rows can repeat or vanish across pages.
            .order_by(Item.created_at.desc(), Item.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await self._session.execute(page_stmt)).scalars().all())
        return rows, total

    def add(self, item: Item) -> Item:
        self._session.add(item)
        return item

    async def flush(self) -> None:
        await self._session.flush()

    async def delete(self, item: Item) -> None:
        await self._session.delete(item)
