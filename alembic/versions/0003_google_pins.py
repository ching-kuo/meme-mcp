"""google_pins table for trust-on-first-use sub->email pinning

Revision ID: 0003_google_pins
Revises: 0002_vector_ddl
Create Date: 2026-06-03

Mirrors the inline DDL in SQLiteGooglePinStore as defense-in-depth so a fresh DB
walked through migrations matches the store's self-created shape exactly. The
email UNIQUE constraint enforces first-sign-in-wins.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0003_google_pins"
down_revision: str | Sequence[str] | None = "0002_vector_ddl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS google_pins (
            sub TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS google_pins")
