"""Trust-on-first-use ``sub -> email`` pins for Google friends.

The operator invites a friend by their Google account email (``google:<email>``
in the allowlist). On the first verified Google sign-in (any mailbox, not only
Gmail) the app records a durable pin binding the immutable Google ``sub`` to
that invited mailbox; the principal that PATs and audit bind to is then
``google:<sub>``, never the mutable email. Authorization for a returning friend
resolves ``sub -> email`` here and checks the email against the allowlist, so an
email rename does not revoke access (the pinned email is the operator's invite).

The ``email UNIQUE`` constraint enforces first-sign-in-wins: a second ``sub``
presenting an already-pinned email is rejected at the DB layer. Eviction
(``delete_*``) is terminal -- re-inviting the same email requires a fresh first
sign-in and cannot reactivate the previously pinned ``sub`` (R13).

Like the PAT store, this uses a local SQLite file regardless of DATABASE_URL
dialect and self-creates its table as defense-in-depth (the Alembic migration
mirrors the shape).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


class SQLiteGooglePinStore:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS google_pins (
                    sub TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def email_for_sub(self, sub: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT email FROM google_pins WHERE sub = ?", (sub,)
            ).fetchone()
        return None if row is None else str(row[0])

    def sub_for_email(self, email: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sub FROM google_pins WHERE email = ?", (email,)
            ).fetchone()
        return None if row is None else str(row[0])

    def create_pin(self, sub: str, email: str) -> bool:
        """Pin ``sub -> email`` on first sign-in (insert-if-absent).

        Returns True if ``sub`` is (now) pinned to ``email``; False when the email
        is already pinned to a *different* sub. The ``email UNIQUE`` constraint is
        the authoritative first-sign-in-wins guard (the IntegrityError catch), so
        concurrent first-logins for the same invited email converge to one pin.
        Idempotent: a repeat sign-in by the same sub returns True.
        """
        now = self._clock().isoformat()
        with self._connect() as conn:
            # One atomic write: INSERT OR IGNORE swallows BOTH the sub-PK conflict
            # (idempotent / concurrent same-sub first sign-in) and the email-UNIQUE
            # conflict (a second sub for an already-pinned email, first-sign-in-wins).
            # The post-write read of the sub's pinned email then disambiguates: it
            # equals `email` only when this sub owns that mailbox.
            conn.execute(
                "INSERT OR IGNORE INTO google_pins (sub, email, created_at) VALUES (?, ?, ?)",
                (sub, email, now),
            )
            row = conn.execute(
                "SELECT email FROM google_pins WHERE sub = ?", (sub,)
            ).fetchone()
        return row is not None and str(row[0]) == email

    def delete_by_email(self, email: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM google_pins WHERE email = ?", (email,))
        return cursor.rowcount > 0

    def delete_by_sub(self, sub: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM google_pins WHERE sub = ?", (sub,))
        return cursor.rowcount > 0

    def all_pins(self) -> list[tuple[str, str, str]]:
        """Every (sub, email, created_at), newest first -- for ``meme-mcp pin list``."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT sub, email, created_at FROM google_pins ORDER BY created_at DESC"
            ).fetchall()
        return [(str(sub), str(email), str(created_at)) for sub, email, created_at in rows]
