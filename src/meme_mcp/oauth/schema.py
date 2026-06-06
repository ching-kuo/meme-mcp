"""OAuth authorization-server table DDL (self-creating defense-in-depth).

The same shape is owned authoritatively by the Alembic migration
(``0004_oauth``); :class:`~meme_mcp.oauth.store.SQLiteOAuthStore` runs these
``CREATE TABLE IF NOT EXISTS`` statements at construction so a fresh local
SQLite file works without a migration step, mirroring the PAT/pin stores. Kept
in its own module so the token-logic file is not padded by ~80 lines of inert
SQL.
"""

from __future__ import annotations

TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id TEXT PRIMARY KEY,
        client_info TEXT NOT NULL,
        client_secret_encrypted TEXT,
        client_secret_expires_at INTEGER,
        registered_at TEXT NOT NULL,
        last_used_at TEXT
    )
    """,
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
    """,
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
    """,
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
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_client_approvals (
        principal TEXT NOT NULL,
        client_id TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        PRIMARY KEY (principal, client_id)
    )
    """,
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
    """,
)
