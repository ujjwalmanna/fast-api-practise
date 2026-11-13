"""Custom SQLAlchemy column types.

## Why `UtcDateTime` exists

`DateTime(timezone=True)` is a *request*, not a guarantee. PostgreSQL honours
it and returns timezone-aware datetimes. **SQLite has no timezone support at
all**: it stores the value as a string and returns a **naive** `datetime`. So
this, which looks obviously correct, raises at runtime on SQLite:

```python
if token.expires_at <= datetime.now(timezone.utc):   # TypeError
    ...
```

    TypeError: can't compare offset-naive and offset-aware datetimes

Worse, the failure is data-dependent and backend-dependent: it passes on
PostgreSQL in CI and breaks on SQLite locally, or vice versa. And if both sides
happen to be naive, the comparison silently succeeds while treating a UTC
timestamp as local time - an off-by-hours bug that no test notices.

`UtcDateTime` removes the whole class of problem:

* on the way **in**, a naive value is assumed to be UTC and an aware value is
  converted to UTC, so the database only ever holds UTC;
* on the way **out**, a naive value is re-stamped as UTC.

Every datetime in the application is therefore aware and in UTC, on every
backend. The rendered DDL is unchanged (`DateTime(timezone=True)`), so this
needs no migration.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect


class UtcDateTime(TypeDecorator[dt.datetime]):
    """A timezone-aware `DateTime` that is always UTC, on every backend."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        """Normalise to UTC before storing."""
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            # A naive value reaching the database is a bug upstream, but
            # assuming UTC is strictly better than storing an unknown offset.
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    def process_result_value(
        self, value: dt.datetime | None, dialect: Dialect
    ) -> dt.datetime | None:
        """Guarantee the value handed back to Python is aware and UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            # The SQLite case: the column was declared timezone=True but the
            # driver returns naive. We know it was stored as UTC.
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)
