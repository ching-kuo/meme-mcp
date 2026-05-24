from __future__ import annotations

from dataclasses import dataclass

from meme_mcp.auth.pat import InMemoryPatStore, SQLitePatStore, verify_pat
from meme_mcp.errors import ErrorCode, MemeMCPError


@dataclass(frozen=True)
class Friend:
    github_login: str


def require_operator(user: Friend, operator_login: str) -> Friend:
    if user.github_login != operator_login:
        raise MemeMCPError(ErrorCode.FORBIDDEN, [{"field": "user", "reason": "operator_required"}])
    return user


def require_pat(
    authorization: str | None,
    pat_store: InMemoryPatStore | SQLitePatStore,
    allowlist: set[str],
    pepper: str,
) -> Friend:
    if authorization is None or not authorization.startswith("Bearer "):
        raise MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "authorization", "reason": "bearer"}])
    token = authorization.removeprefix("Bearer ").strip()
    login = verify_pat(pat_store, token, pepper)
    if login is None:
        raise MemeMCPError(
            ErrorCode.UNAUTHORIZED,
            [{"field": "authorization", "reason": "invalid"}],
        )
    if login not in allowlist:
        raise MemeMCPError(
            ErrorCode.FORBIDDEN_NOT_ALLOWLISTED,
            [{"field": "github_login", "reason": "not_allowlisted"}],
        )
    return Friend(login)
