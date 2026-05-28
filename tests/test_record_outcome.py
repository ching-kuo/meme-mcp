from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI

from meme_mcp.app import AppMCPBackend, create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.engine import sqlite_path
from meme_mcp.db.outcomes import OutcomeEventStore
from meme_mcp.errors import ErrorCode, MemeMCPError
from tests.test_upload_flow import good_settings


def _app_with_friend(tmp_path: Path, capability: str = "readwrite") -> tuple[FastAPI, str]:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(
        app.state.pat_store,
        "alice",
        app.state.pat_hash_pepper_value,
        capability=capability,  # type: ignore[arg-type]
    )
    app.state.allowlist.add("alice")
    return app, token


def _db_path_for(app: FastAPI) -> Path:
    return sqlite_path(
        app.state.settings.database_url,
        Path(app.state.settings.storage_dir) / "meme.db",
    )


def test_app_backend_records_event_for_valid_outcome(tmp_path: Path) -> None:
    app, _ = _app_with_friend(tmp_path)
    envelope = AppMCPBackend(app).record_outcome("drake", "used", "alice")
    assert envelope["ok"] is True
    assert envelope["data"] == {"template_id": "drake", "outcome": "used"}
    with sqlite3.connect(_db_path_for(app)) as conn:
        row = conn.execute(
            "SELECT template_id, actor, outcome FROM outcome_events"
        ).fetchone()
    assert row == ("drake", "alice", "used")


def test_app_backend_rejects_invalid_outcome(tmp_path: Path) -> None:
    app, _ = _app_with_friend(tmp_path)
    with pytest.raises(MemeMCPError) as info:
        AppMCPBackend(app).record_outcome("drake", "loved", "alice")
    assert info.value.error_code is ErrorCode.INVALID_INPUT


def test_app_backend_persists_unknown_template_id(tmp_path: Path) -> None:
    """template_id is not foreign-keyed; record_outcome is a signal, not a referential
    constraint. Calls referencing an unknown template still persist so retrieval can
    learn from drift."""
    app, _ = _app_with_friend(tmp_path)
    AppMCPBackend(app).record_outcome("never-seen", "used", "alice")
    store = OutcomeEventStore(_db_path_for(app))
    assert store.recent_used_count("never-seen") == 1


def test_app_backend_record_outcome_hits_find_limiter(tmp_path: Path) -> None:
    """record_outcome shares the find_limiter bucket so a flood of share-signal calls
    can't bypass the find rate budget; assert the call succeeds and the same limiter
    serves subsequent find calls without raising."""
    app, _ = _app_with_friend(tmp_path)
    AppMCPBackend(app).record_outcome("drake", "used", "alice")
    AppMCPBackend(app).find("drake", None, "alice")
