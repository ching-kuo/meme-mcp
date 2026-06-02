from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meme_mcp.audit.events import MemeEvent
from meme_mcp.auth.pat import SQLitePatStore, hash_pat, verify_pat
from meme_mcp.auth.pat_web import regenerate_web, revoke_web
from meme_mcp.errors import ErrorCode, MemeMCPError


class CaptureSink:
    def __init__(self) -> None:
        self.events: list[MemeEvent] = []

    def emit(self, event: MemeEvent) -> None:
        self.events.append(event)


class RaisingSink:
    def emit(self, event: MemeEvent) -> None:
        del event
        raise RuntimeError("audit unavailable")


def test_regenerate_web_issues_bounded_token_and_audits(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    store = SQLitePatStore(tmp_path / "pats.db", clock=lambda: fixed_now)
    sink = CaptureSink()

    plaintext = regenerate_web(
        store=store,
        friend_login="alice",
        pepper="pepper",
        capability="readwrite",
        ttl_days=90,
        audit_sink=sink,
    )

    assert verify_pat(store, plaintext, "pepper") == ("github:alice", "readwrite")
    status = store.current_status("alice")
    assert status.expires_at == fixed_now + timedelta(days=90)
    assert sink.events == [
        MemeEvent(
            event_type="pat_issued",
            actor="alice",
            outcome="success",
            payload={"scope": "readwrite", "expires_in_days": 90},
        )
    ]
    assert plaintext not in sink.events[0].payload.values()


def test_regenerate_web_supports_read_scope_30_day_expiry(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
    store = SQLitePatStore(tmp_path / "pats.db", clock=lambda: fixed_now)

    plaintext = regenerate_web(
        store=store,
        friend_login="alice",
        pepper="pepper",
        capability="read",
        ttl_days=30,
        audit_sink=None,
    )

    assert verify_pat(store, plaintext, "pepper") == ("github:alice", "read")
    assert store.current_status("alice").expires_at == fixed_now + timedelta(days=30)


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("ttl_days", {"capability": "read", "ttl_days": 0}),
        ("ttl_days", {"capability": "read", "ttl_days": 7}),
        ("scope", {"capability": "admin", "ttl_days": 30}),
    ],
)
def test_regenerate_web_rejects_invalid_inputs(
    tmp_path: Path, field: str, kwargs: dict[str, object]
) -> None:
    store = SQLitePatStore(tmp_path / "pats.db")
    sink = CaptureSink()

    with pytest.raises(MemeMCPError) as exc:
        regenerate_web(
            store=store,
            friend_login="alice",
            pepper="pepper",
            audit_sink=sink,
            **kwargs,
        )

    assert exc.value.error_code == ErrorCode.INVALID_INPUT
    assert exc.value.errors[0]["field"] == field
    assert store.current_status("alice").state == "none"
    assert sink.events == []


def test_audit_events_never_carry_plaintext_or_digest(tmp_path: Path) -> None:
    store = SQLitePatStore(tmp_path / "pats.db")
    sink = CaptureSink()

    plaintext = regenerate_web(
        store=store,
        friend_login="alice",
        pepper="pepper",
        capability="readwrite",
        ttl_days=90,
        audit_sink=sink,
    )
    revoke_web(store=store, friend_login="alice", audit_sink=sink)

    digest = hash_pat(plaintext, "pepper")
    for event in sink.events:
        serialized = json.dumps(event.to_jsonable())
        assert plaintext not in serialized
        assert digest not in serialized


def test_revoke_web_audits_success_and_noop(tmp_path: Path) -> None:
    store = SQLitePatStore(tmp_path / "pats.db")
    sink = CaptureSink()
    regenerate_web(
        store=store,
        friend_login="alice",
        pepper="pepper",
        capability="read",
        ttl_days=30,
        audit_sink=None,
    )

    assert revoke_web(store=store, friend_login="alice", audit_sink=sink) is True
    assert revoke_web(store=store, friend_login="alice", audit_sink=sink) is False

    assert [event.event_type for event in sink.events] == ["pat_revoked", "pat_revoked"]
    assert [event.payload["active_token_revoked"] for event in sink.events] == [True, False]
    assert [event.outcome for event in sink.events] == ["success", "failed_validation"]


def test_audit_failure_does_not_block(tmp_path: Path) -> None:
    store = SQLitePatStore(tmp_path / "pats.db")

    plaintext = regenerate_web(
        store=store,
        friend_login="alice",
        pepper="pepper",
        capability="read",
        ttl_days=30,
        audit_sink=RaisingSink(),
    )

    assert verify_pat(store, plaintext, "pepper") == ("github:alice", "read")
    assert revoke_web(store=store, friend_login="alice", audit_sink=RaisingSink()) is True
