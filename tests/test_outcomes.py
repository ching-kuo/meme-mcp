from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meme_mcp.db.outcomes import OutcomeEventStore


def test_record_persists_with_clock_timestamp(tmp_path: Path) -> None:
    fixed = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    store = OutcomeEventStore(tmp_path / "out.db", clock=lambda: fixed)
    store.record("drake", "alice", "used")
    with sqlite3.connect(tmp_path / "out.db") as conn:
        row = conn.execute("SELECT template_id, actor, outcome, ts FROM outcome_events").fetchone()
    assert row == ("drake", "alice", "used", fixed.isoformat())


def test_record_rejects_invalid_outcome(tmp_path: Path) -> None:
    store = OutcomeEventStore(tmp_path / "out.db")
    with pytest.raises(ValueError, match="outcome"):
        store.record("drake", "alice", "loved")  # type: ignore[arg-type]


def test_sql_check_constraint_catches_corrupt_outcome(tmp_path: Path) -> None:
    OutcomeEventStore(tmp_path / "out.db")
    with sqlite3.connect(tmp_path / "out.db") as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO outcome_events (template_id, actor, outcome, ts) "
            "VALUES (?, ?, ?, ?)",
            ("drake", "alice", "loved", datetime.now(UTC).isoformat()),
        )


def test_recent_used_count_respects_window(tmp_path: Path) -> None:
    now = datetime(2026, 5, 28, tzinfo=UTC)
    clock = [now]
    store = OutcomeEventStore(tmp_path / "out.db", clock=lambda: clock[0])
    store.record("drake", "alice", "used", ts=now - timedelta(days=5))
    store.record("drake", "bob", "used", ts=now - timedelta(days=29))
    store.record("drake", "carol", "used", ts=now - timedelta(days=31))
    assert store.recent_used_count("drake", window_days=30) == 2
    assert store.recent_used_count("drake", window_days=7) == 1
    assert store.recent_used_count("drake", window_days=0) == 0


def test_recent_used_count_ignores_other_outcomes(tmp_path: Path) -> None:
    store = OutcomeEventStore(tmp_path / "out.db")
    store.record("drake", "alice", "sent")
    store.record("drake", "bob", "dropped")
    store.record("drake", "carol", "used")
    assert store.recent_used_count("drake") == 1


def test_recent_used_count_returns_zero_for_unknown_template(tmp_path: Path) -> None:
    store = OutcomeEventStore(tmp_path / "out.db")
    assert store.recent_used_count("never-seen") == 0


def test_prune_removes_old_events(tmp_path: Path) -> None:
    now = datetime(2026, 5, 28, tzinfo=UTC)
    clock = [now]
    store = OutcomeEventStore(tmp_path / "out.db", clock=lambda: clock[0])
    store.record("drake", "a", "used", ts=now - timedelta(days=200))
    store.record("drake", "b", "used", ts=now - timedelta(days=10))
    deleted = store.prune(older_than_days=180)
    assert deleted == 1
    assert store.recent_used_count("drake", window_days=365) == 1


def test_idempotent_schema_creation(tmp_path: Path) -> None:
    db_path = tmp_path / "out.db"
    OutcomeEventStore(db_path)
    OutcomeEventStore(db_path)  # no-op
    with sqlite3.connect(db_path) as conn:
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(outcome_events)")}
    assert "outcome_events_template_ts" in indexes
