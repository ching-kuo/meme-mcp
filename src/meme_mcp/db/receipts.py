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

    def hashes_older_than(self, cutoff: datetime) -> list[str]:
        """Return rendered_hash values for receipts created at or before `cutoff`."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hash FROM generated_receipts WHERE created_at <= ? ORDER BY created_at",
                (cutoff.isoformat(),),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def all_hashes_oldest_first(self) -> list[tuple[str, datetime]]:
        """Return (hash, created_at) tuples for every receipt, oldest first; used by the
        max-bytes LRU eviction path in render GC."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hash, created_at FROM generated_receipts ORDER BY created_at"
            ).fetchall()
        return [(str(h), datetime.fromisoformat(str(ts))) for h, ts in rows]

    def delete(self, rendered_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM generated_receipts WHERE hash = ?", (rendered_hash,))
