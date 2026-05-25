from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class PendingUpload:
    upload_id: str
    friend_login: str
    image_path: str
    metadata: dict[str, Any]
    slot_definitions: list[dict[str, Any]]
    exact_hash: str
    perceptual_hash: str
    duplicate_action: str
    duplicate_template_id: str | None
    suspect_flags: list[str]


class PendingUploadStore:
    def __init__(
        self,
        path: str | Path,
        *,
        ttl: timedelta = DEFAULT_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_uploads)")}
            if "expires_at" not in columns:
                # Migrate pre-TTL DBs: backfill expires_at from created_at + ttl, then NOT NULL.
                conn.execute("ALTER TABLE pending_uploads ADD COLUMN expires_at TEXT")
                conn.execute(
                    "UPDATE pending_uploads SET expires_at = ? WHERE expires_at IS NULL",
                    (self._clock().isoformat(),),
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def create(
        self,
        *,
        friend_login: str,
        image_path: str,
        metadata: dict[str, Any],
        slot_definitions: list[dict[str, Any]],
        exact_hash: str,
        perceptual_hash: str,
        duplicate_action: str,
        duplicate_template_id: str | None,
        suspect_flags: list[str],
    ) -> PendingUpload:
        upload_id = uuid.uuid4().hex
        now = self._clock()
        expires_at = now + self.ttl
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_uploads (
                    id, friend_login, image_path, metadata_json, slot_definitions_json,
                    exact_hash, perceptual_hash, duplicate_action, duplicate_template_id,
                    suspect_flags_json, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    friend_login,
                    image_path,
                    json.dumps(metadata, sort_keys=True),
                    json.dumps(slot_definitions, sort_keys=True),
                    exact_hash,
                    perceptual_hash,
                    duplicate_action,
                    duplicate_template_id,
                    json.dumps(suspect_flags),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
        return PendingUpload(
            upload_id=upload_id,
            friend_login=friend_login,
            image_path=image_path,
            metadata=metadata,
            slot_definitions=slot_definitions,
            exact_hash=exact_hash,
            perceptual_hash=perceptual_hash,
            duplicate_action=duplicate_action,
            duplicate_template_id=duplicate_template_id,
            suspect_flags=suspect_flags,
        )

    def get(self, upload_id: str, friend_login: str) -> PendingUpload:
        now_iso = self._clock().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, friend_login, image_path, metadata_json, slot_definitions_json,
                       exact_hash, perceptual_hash, duplicate_action, duplicate_template_id,
                       suspect_flags_json
                FROM pending_uploads
                WHERE id = ? AND friend_login = ? AND expires_at > ?
                """,
                (upload_id, friend_login, now_iso),
            ).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return PendingUpload(
            upload_id=str(row[0]),
            friend_login=str(row[1]),
            image_path=str(row[2]),
            metadata=json.loads(str(row[3])),
            slot_definitions=json.loads(str(row[4])),
            exact_hash=str(row[5]),
            perceptual_hash=str(row[6]),
            duplicate_action=str(row[7]),
            duplicate_template_id=None if row[8] is None else str(row[8]),
            suspect_flags=json.loads(str(row[9])),
        )

    def delete(self, upload_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_uploads WHERE id = ?", (upload_id,))

    def cleanup_expired(self) -> int:
        now_iso = self._clock().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM pending_uploads WHERE expires_at <= ?",
                (now_iso,),
            )
            return cursor.rowcount
