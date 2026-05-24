import json

from meme_mcp.audit.events import MemeEvent
from meme_mcp.audit.sink import JsonlAuditSink


def test_audit_writes_jsonl_without_raw_query(tmp_path) -> None:
    sink = JsonlAuditSink(tmp_path / "audit.jsonl")
    sink.emit(
        MemeEvent(
            event_type="find",
            actor="alice",
            outcome="success",
            payload={"query_len": len("super secret"), "candidates_returned": 1},
        )
    )
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "super secret" not in raw
    parsed = json.loads(raw)
    assert parsed["v"] == 1
    assert parsed["source"] == "meme-mcp"
    assert parsed["event_type"] == "find"


def test_audit_rotates_and_sets_private_mode(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    sink = JsonlAuditSink(path, max_bytes=1)
    event = MemeEvent("find", "alice", "success", {"query_len": 1})
    sink.emit(event)
    sink.emit(event)
    assert path.exists()
    assert path.with_suffix(".jsonl.1").exists()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
