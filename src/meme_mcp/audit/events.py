from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

EventType = Literal[
    "find",
    "generate",
    "upload",
    "pat_issued",
    "pat_revoked",
    "pat_validation_failed",
    "oauth_login",
    "oauth_denied",
    "allowlist_modified",
    "pepper_rotated",
    "doctor_run",
]
Outcome = Literal[
    "success",
    "failed_validation",
    "failed_auth",
    "failed_upstream",
    "dry_run",
    "rate_limited",
]


@dataclass(frozen=True)
class MemeEvent:
    event_type: EventType
    actor: str
    outcome: Outcome
    payload: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "v": 1,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "source": "meme-mcp",
            "event_type": self.event_type,
            "actor": self.actor,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            **self.payload,
        }

