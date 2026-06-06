"""OAuth 2.1 authorization-server tables (clients, codes, tokens, approvals, pending)

Revision ID: 0004_oauth
Revises: 0003_google_pins
Create Date: 2026-06-06

Mirrors the inline DDL in SQLiteOAuthStore as defense-in-depth so a fresh DB
walked through migrations matches the store's self-created shape exactly. Token,
code, and nonce columns hold one-way HMAC hashes; a client secret (confidential
clients only) is stored encrypted in client_secret_encrypted. Nothing here is
plaintext.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0004_oauth"
down_revision: str | Sequence[str] | None = "0003_google_pins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id TEXT PRIMARY KEY,
            client_info TEXT NOT NULL,
            client_secret_encrypted TEXT,
            client_secret_expires_at INTEGER,
            registered_at TEXT NOT NULL,
            last_used_at TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_auth_codes (
            code_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            redirect_uri_provided_explicitly INTEGER NOT NULL,
            code_challenge TEXT NOT NULL,
            scopes TEXT NOT NULL,
            principal TEXT NOT NULL,
            resource TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
            token_hash TEXT PRIMARY KEY,
            family_id TEXT NOT NULL,
            prev_token_hash TEXT,
            client_id TEXT NOT NULL,
            principal TEXT NOT NULL,
            scopes TEXT NOT NULL,
            resource TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            rotated_at TEXT,
            revoked_at TEXT,
            superseded_by_hash TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_access_tokens (
            token_hash TEXT PRIMARY KEY,
            family_id TEXT NOT NULL,
            client_id TEXT NOT NULL,
            principal TEXT NOT NULL,
            scopes TEXT NOT NULL,
            resource TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_client_approvals (
            principal TEXT NOT NULL,
            client_id TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            PRIMARY KEY (principal, client_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_pending_requests (
            nonce_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            redirect_uri_provided_explicitly INTEGER NOT NULL,
            code_challenge TEXT NOT NULL,
            scopes TEXT NOT NULL,
            resource TEXT,
            state TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )


def downgrade() -> None:
    for table in (
        "oauth_pending_requests",
        "oauth_client_approvals",
        "oauth_access_tokens",
        "oauth_refresh_tokens",
        "oauth_auth_codes",
        "oauth_clients",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
