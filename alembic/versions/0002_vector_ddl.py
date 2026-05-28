"""dialect-conditional pgvector DDL

Revision ID: 0002_vector_ddl
Revises: 0001_baseline
Create Date: 2026-05-28

On Postgres: install the pgvector extension, recreate template_vectors with a vector(1536)
column, and add an ivfflat cosine index. On SQLite (and any other dialect): no-op because
the baseline already created the portable JSON-backed template_vectors table.
"""
from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "0002_vector_ddl"
down_revision: Union[str, Sequence[str], None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _dialect() -> str:
    bind = op.get_bind()
    return bind.dialect.name


def upgrade() -> None:
    if _dialect() != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("DROP TABLE IF EXISTS template_vectors")
    op.execute(
        """
        CREATE TABLE template_vectors (
            template_id TEXT PRIMARY KEY,
            embedding vector(1536) NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS template_vectors_embedding_idx "
        "ON template_vectors USING ivfflat (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS template_vectors_embedding_idx")
    op.execute("DROP TABLE IF EXISTS template_vectors")
    op.execute(
        """
        CREATE TABLE template_vectors (
            template_id TEXT PRIMARY KEY,
            vector_json TEXT NOT NULL,
            dimensions INTEGER NOT NULL
        )
        """
    )
