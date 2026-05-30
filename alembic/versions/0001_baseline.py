"""baseline schema captured from v1 inline DDL

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-28

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS templates (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            slot_definitions_json TEXT NOT NULL,
            image_path TEXT NOT NULL,
            perceptual_hash TEXT NOT NULL,
            exact_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            friend_login TEXT NOT NULL,
            pat_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            capability TEXT NOT NULL DEFAULT 'readwrite',
            last_used_at TEXT,
            revoked_at TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_uploads (
            id TEXT PRIMARY KEY,
            friend_login TEXT NOT NULL,
            image_path TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            slot_definitions_json TEXT NOT NULL,
            exact_hash TEXT NOT NULL,
            perceptual_hash TEXT NOT NULL,
            duplicate_action TEXT NOT NULL,
            duplicate_template_id TEXT,
            suspect_flags_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS template_vectors (
            template_id TEXT PRIMARY KEY,
            vector_json TEXT NOT NULL,
            dimensions INTEGER NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS template_embeddings_meta (
            template_id TEXT PRIMARY KEY,
            embedding_model TEXT NOT NULL,
            embedded_text_hash TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS generated_receipts (
            hash TEXT PRIMARY KEY,
            template_id TEXT NOT NULL,
            friend_login TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS outcome_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('used','sent','dropped')),
            ts TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS outcome_events_template_ts "
        "ON outcome_events(template_id, ts)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS outcome_events_template_ts")
    for table in (
        "outcome_events",
        "generated_receipts",
        "template_embeddings_meta",
        "template_vectors",
        "pending_uploads",
        "pats",
        "templates",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
