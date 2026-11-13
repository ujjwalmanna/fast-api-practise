"""Model registry.

Importing every model in one module solves two real problems:

1. **Alembic autogenerate.** `Base.metadata` only knows about mapped classes
   whose modules have been imported. A model that nothing imports is invisible
   to autogenerate, which then cheerfully emits a migration that drops the
   table - or omits a new one entirely.
2. **Relationship resolution.** SQLAlchemy resolves string-based relationships
   (`Mapped["User"]`) against its class registry at mapper-configuration time.
   If `Item` is imported but `User` is not, that lookup fails.

`migrations/env.py` imports this module for exactly that reason. Add every new
model here.
"""

from api_framework.db.base import Base
from api_framework.domain.items.models import Item
from api_framework.domain.users.models import RefreshToken, User

__all__ = ["Base", "Item", "RefreshToken", "User"]
