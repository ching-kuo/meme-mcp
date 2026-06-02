from __future__ import annotations

from dataclasses import dataclass

from meme_mcp.auth.authorization import (
    SupportsAllowlist,
    SupportsPinLookup,
    is_authorized,
    normalize_principal,
)
from meme_mcp.auth.pat import (
    DEFAULT_CAPABILITY,
    Capability,
    InMemoryPatStore,
    SQLitePatStore,
    verify_pat,
)
from meme_mcp.errors import ErrorCode, MemeMCPError


@dataclass(frozen=True)
class Friend:
    # Provider-namespaced principal: ``github:<login>`` or ``google:<sub>``.
    principal: str
    capability: Capability = DEFAULT_CAPABILITY


def require_operator(user: Friend, operator_login: str) -> Friend:
    # The operator is configured as a bare GitHub login; normalize it so it
    # compares against the namespaced principal a Friend now carries.
    if user.principal != normalize_principal(operator_login):
        raise MemeMCPError(ErrorCode.FORBIDDEN, [{"field": "user", "reason": "operator_required"}])
    return user


def require_write(friend: Friend) -> Friend:
    if friend.capability != "readwrite":
        raise MemeMCPError(
            ErrorCode.UNAUTHORIZED,
            [{"field": "capability", "reason": "write_scope_required"}],
        )
    return friend


def require_pat(
    authorization: str | None,
    pat_store: InMemoryPatStore | SQLitePatStore,
    allowlist: SupportsAllowlist,
    pepper: str,
    pin_store: SupportsPinLookup | None = None,
) -> Friend:
    if authorization is None or not authorization.startswith("Bearer "):
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "authorization", "reason": "bearer"}])
    token = authorization.removeprefix("Bearer ").strip()
    result = verify_pat(pat_store, token, pepper)
    if result is None:
        raise MemeMCPError(
            ErrorCode.UNAUTHORIZED,
            [{"field": "authorization", "reason": "invalid"}],
        )
    principal, capability = result
    if not is_authorized(principal, allowlist=allowlist, pin_store=pin_store):
        raise MemeMCPError(
            ErrorCode.FORBIDDEN_NOT_ALLOWLISTED,
            [{"field": "principal", "reason": "not_allowlisted"}],
        )
    return Friend(principal, capability)
