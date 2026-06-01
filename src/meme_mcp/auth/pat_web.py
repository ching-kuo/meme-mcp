from __future__ import annotations

from typing import Protocol

from meme_mcp.audit.events import MemeEvent
from meme_mcp.auth.pat import Capability, SQLitePatStore, issue_pat
from meme_mcp.errors import ErrorCode, MemeMCPError

WEB_TTL_DAYS = (30, 90, 365)


class AuditSink(Protocol):
    def emit(self, event: MemeEvent) -> None: ...


def regenerate_web(
    *,
    store: SQLitePatStore,
    friend_login: str,
    pepper: str,
    capability: object,
    ttl_days: object,
    audit_sink: AuditSink | None,
) -> str:
    scoped = _validate_capability(capability)
    ttl = _validate_ttl(ttl_days)
    plaintext = issue_pat(
        store,
        friend_login,
        pepper,
        ttl_days=ttl,
        capability=scoped,
    )
    _emit(
        audit_sink,
        MemeEvent(
            event_type="pat_issued",
            actor=friend_login,
            outcome="success",
            payload={"scope": scoped, "expires_in_days": ttl},
        ),
    )
    return plaintext


def revoke_web(
    *,
    store: SQLitePatStore,
    friend_login: str,
    audit_sink: AuditSink | None,
) -> bool:
    revoked = store.revoke_active(friend_login)
    _emit(
        audit_sink,
        MemeEvent(
            event_type="pat_revoked",
            actor=friend_login,
            outcome="success" if revoked else "failed_validation",
            payload={"active_token_revoked": revoked},
        ),
    )
    return revoked


def _validate_capability(raw: object) -> Capability:
    if raw == "read":
        return "read"
    if raw == "readwrite":
        return "readwrite"
    raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "scope", "reason": "invalid"}])


def _validate_ttl(raw: object) -> int:
    if isinstance(raw, bool):
        ttl = -1
    elif isinstance(raw, int):
        ttl = raw
    elif isinstance(raw, str):
        try:
            ttl = int(raw)
        except ValueError:
            ttl = -1
    else:
        ttl = -1
    if ttl not in WEB_TTL_DAYS:
        raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "ttl_days", "reason": "invalid"}])
    return ttl


def _emit(audit_sink: AuditSink | None, event: MemeEvent) -> None:
    if audit_sink is None:
        return
    try:
        audit_sink.emit(event)
    except Exception:
        return
