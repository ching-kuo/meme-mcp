from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class ReceiptStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generated_receipts (
                    hash TEXT PRIMARY KEY,
                    template_id TEXT NOT NULL,
                    friend_login TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(self, rendered_hash: str, template_id: str, friend_login: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO generated_receipts
                (hash, template_id, friend_login, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (rendered_hash, template_id, friend_login, datetime.now(UTC).isoformat()),
            )

    def exists_for_friend(self, rendered_hash: str, friend_login: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM generated_receipts WHERE hash = ? AND friend_login = ?",
                (rendered_hash, friend_login),
            ).fetchone()
            return row is not None
