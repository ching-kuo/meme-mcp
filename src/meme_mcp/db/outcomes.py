"""Agent-reported share-signal outcomes feeding retrieval re-ranking (U8) and the
record_outcome MCP tool (U7). The event shape intentionally mirrors share intent
(used / sent / dropped) rather than per-call success status, which lives in audit.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

Outcome = Literal["used", "sent", "dropped"]
VALID_OUTCOMES: tuple[Outcome, ...] = ("used", "sent", "dropped")


@dataclass(frozen=True)
class OutcomeEvent:
    template_id: str
    actor: str
    outcome: Outcome
    ts: datetime


class OutcomeEventStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS outcome_events_template_ts "
                "ON outcome_events(template_id, ts)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(
        self,
        template_id: str,
        actor: str,
        outcome: Outcome,
        *,
        ts: datetime | None = None,
    ) -> None:
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"outcome must be one of {VALID_OUTCOMES}, got {outcome!r}")
        stamp = (ts or self._clock()).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO outcome_events (template_id, actor, outcome, ts) "
                "VALUES (?, ?, ?, ?)",
                (template_id, actor, outcome, stamp),
            )

    def recent_used_count(self, template_id: str, *, window_days: int = 30) -> int:
        if window_days < 0:
            raise ValueError(f"window_days must be >= 0, got {window_days}")
        cutoff = (self._clock() - timedelta(days=window_days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM outcome_events "
                "WHERE template_id = ? AND outcome = 'used' AND ts > ?",
                (template_id, cutoff),
            ).fetchone()
        return int(row[0]) if row else 0

    def prune(self, *, older_than_days: int) -> int:
        if older_than_days < 0:
            raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")
        cutoff = (self._clock() - timedelta(days=older_than_days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM outcome_events WHERE ts <= ?",
                (cutoff,),
            )
            return cursor.rowcount
