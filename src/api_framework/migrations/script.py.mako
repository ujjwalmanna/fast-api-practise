"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Review checklist before merging this migration:
  - Is it reversible? Implement `downgrade()` or state why it cannot be.
  - Is it safe to run while the old code is still serving traffic?
    Adding a NOT NULL column without a server_default breaks running pods.
    Prefer: add nullable -> backfill -> set NOT NULL in a later release.
  - Will it lock a large table? On PostgreSQL, CREATE INDEX CONCURRENTLY.
  - Does a data backfill need batching to avoid a long transaction?
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
