"""Item model - the worked example of a domain resource."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from api_framework.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "items"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # Numeric, never Float. Binary floating point cannot represent 0.10
    # exactly, so money in a float silently drifts. `asdecimal=True` returns
    # Decimal in Python.
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    owner_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    __table_args__ = (
        # Enforce the invariant in the database too. Application validation can
        # be bypassed - by a migration, a fixture, or another service - and the
        # DB is the last line of defence. Named, so SQLite batch mode can
        # rebuild the table (see db/base.py).
        CheckConstraint("price >= 0", name="price_non_negative"),
        # Composite index for the common query: this owner's items, newest
        # first. Column order matters - owner_id is the equality predicate, so
        # it must come first.
        Index("ix_items_owner_id_created_at", "owner_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Item id={self.id} name={self.name!r}>"
