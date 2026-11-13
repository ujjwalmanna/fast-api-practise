"""Item API schemas."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

T = TypeVar("T")

# Reusable constrained type: trims whitespace and rejects an all-blank name,
# which `min_length` alone would let through.
ItemName = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class ItemCreate(BaseModel):
    name: ItemName
    description: str | None = Field(default=None, max_length=5000)
    # `max_digits`/`decimal_places` mirror the Numeric(12, 2) column, so an
    # over-precise value is rejected at the edge with a 422 rather than being
    # silently rounded by the database.
    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class ItemUpdate(BaseModel):
    """PATCH payload - all fields optional."""

    name: ItemName | None = None
    description: str | None = Field(default=None, max_length=5000)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class ItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    price: Decimal
    owner_id: str
    created_at: dt.datetime
    updated_at: dt.datetime


class Page(BaseModel, Generic[T]):
    """Generic pagination envelope.

    Returning a bare JSON array is a versioning dead end: adding a total or a
    cursor later is a breaking change. An envelope from day one is free.

    Written with `Generic[T]` rather than PEP 695 `class Page[T]` to keep the
    project importable on Python 3.11.
    """

    items: list[T]
    total: int = Field(description="Total rows matching the query, ignoring pagination")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total
