from __future__ import annotations

import hmac
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from meme_mcp.auth.authorization import (
    normalize_principal,
    principal_in_clause,
    principal_match_values,
)

Capability = Literal["read", "readwrite"]
PatStatusState = Literal["none", "active", "expired", "revoked"]
VALID_CAPABILITIES: tuple[Capability, ...] = ("read", "readwrite")
DEFAULT_TTL_DAYS = 90
DEFAULT_CAPABILITY: Capability = "readwrite"


@dataclass
class PatRecord:
    friend_login: str
    pat_hash: str
    created_at: datetime
    expires_at: datetime | None = None
    capability: Capability = DEFAULT_CAPABILITY
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass(frozen=True)
class PatStatus:
    state: PatStatusState
    capability: Capability | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass
class InMemoryPatStore:
    records: list[PatRecord] = field(default_factory=list)


class SQLitePatStore:
    # Verifier SQL is fixed and unconditional on pat_hash. expires_at, capability, and
    # revoked_at are applied in Python after fetch so the query plan does not differ
    # between "unknown token", "revoked token", and "expired token" (timing-safe lookup).
    _VERIFY_SQL = (
        "SELECT friend_login, expires_at, capability, revoked_at "
        "FROM pats WHERE pat_hash = ?"
    )

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
                CREATE TABLE IF NOT EXISTS pats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    friend_login TEXT NOT NULL,
                    pat_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    capability TEXT NOT NULL DEFAULT 'readwrite',
                    last_used_at TEXT,
                    revoked_at TEXT
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(pats)")}
            if "expires_at" not in columns:
                conn.execute("ALTER TABLE pats ADD COLUMN expires_at TEXT")
            if "capability" not in columns:
                # SQLite does not enforce NOT NULL on ADDed columns without a default at
                # the column level; the literal default below backfills every pre-v1.5
                # row to 'readwrite' so the old behaviour is preserved.
                conn.execute(
                    "ALTER TABLE pats ADD COLUMN capability TEXT NOT NULL DEFAULT 'readwrite'"
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def issue(
        self,
        friend_login: str,
        pat_hash: str,
        *,
        expires_at: datetime | None = None,
        capability: Capability = DEFAULT_CAPABILITY,
    ) -> None:
        now = self._clock().isoformat()
        expires_iso = expires_at.isoformat() if expires_at is not None else None
        # Store the namespaced principal; revoke any prior active PAT for the
        # same principal INCLUDING a legacy bare-login row (no data migration),
        # so a reissue supersedes the friend's pre-namespace PAT and the
        # single-active-PAT invariant holds across the namespace boundary.
        principal = normalize_principal(friend_login)
        placeholders, values = principal_in_clause(principal)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE pats SET revoked_at = ? "
                f"WHERE friend_login IN ({placeholders}) AND revoked_at IS NULL",
                (now, *values),
            )
            conn.execute(
                """
                INSERT INTO pats (friend_login, pat_hash, created_at, expires_at, capability)
                VALUES (?, ?, ?, ?, ?)
                """,
                (principal, pat_hash, now, expires_iso, capability),
            )

    def verify(self, pat_hash: str) -> tuple[str, Capability] | None:
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(self._VERIFY_SQL, (pat_hash,)).fetchone()
            if row is None:
                _timing_safe_compare(pat_hash)
                return None
            login, expires_at_iso, capability_raw, revoked_at = row
            if revoked_at is not None:
                _timing_safe_compare(pat_hash)
                return None
            if expires_at_iso is not None:
                # Parse before comparing — malformed strings would otherwise sort past
                # real ISO timestamps and read as "never expires." Naive datetimes also
                # fail closed since `naive <= aware` raises TypeError, and treating any
                # such record as corrupt is the only safe response.
                try:
                    expires_at = datetime.fromisoformat(str(expires_at_iso))
                except ValueError:
                    _timing_safe_compare(pat_hash)
                    return None
                if expires_at.tzinfo is None or expires_at <= now:
                    _timing_safe_compare(pat_hash)
                    return None
            capability = _coerce_capability(capability_raw)
            if capability is None:
                # Corrupt capability value (somehow outside the enum) — fail closed.
                _timing_safe_compare(pat_hash)
                return None
            try:
                # Normalize a legacy bare login to its namespaced principal; a
                # value that cannot normalize (corrupt/cross-provider) fails closed.
                principal = normalize_principal(str(login))
            except ValueError:
                _timing_safe_compare(pat_hash)
                return None
            conn.execute(
                "UPDATE pats SET last_used_at = ? WHERE pat_hash = ?",
                (now.isoformat(), pat_hash),
            )
            return principal, capability

    def revoke_active(self, friend_login: str) -> bool:
        now = self._clock()
        placeholders, values = principal_in_clause(friend_login)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, expires_at
                FROM pats
                WHERE friend_login IN ({placeholders}) AND revoked_at IS NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
            if row is None:
                return False
            row_id, expires_raw = row
            if _is_expired(expires_raw, now):
                return False
            result = conn.execute(
                "UPDATE pats SET revoked_at = ? WHERE id = ?", (now.isoformat(), row_id)
            )
            return result.rowcount > 0

    def current_status(self, friend_login: str) -> PatStatus:
        now = self._clock()
        placeholders, values = principal_in_clause(friend_login)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT expires_at, capability, revoked_at, last_used_at
                FROM pats
                WHERE friend_login IN ({placeholders})
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
        if row is None:
            return PatStatus("none")
        expires_raw, capability_raw, revoked_raw, last_used_raw = row
        capability = _coerce_capability(capability_raw)
        expires_at = _parse_optional_iso(expires_raw)
        last_used_at = _parse_optional_iso(last_used_raw)
        # Derive "revoked" from the raw column, not a parsed datetime: a non-null
        # but malformed revoked_at must still read as revoked, matching verify's
        # fail-closed `revoked_at is not None` check on the same raw value.
        if revoked_raw is not None:
            state: PatStatusState = "revoked"
        elif capability is None or _is_expired(expires_raw, now):
            state = "expired"
        else:
            state = "active"
        return PatStatus(
            state=state,
            capability=capability,
            expires_at=expires_at,
            last_used_at=last_used_at,
        )


def hash_pat(plaintext: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), plaintext.encode(), "sha256").hexdigest()


def issue_pat(
    store: InMemoryPatStore | SQLitePatStore,
    friend_login: str,
    pepper: str,
    *,
    ttl_days: int | None = DEFAULT_TTL_DAYS,
    capability: Capability = DEFAULT_CAPABILITY,
) -> str:
    if capability not in VALID_CAPABILITIES:
        raise ValueError(
            f"capability must be one of {VALID_CAPABILITIES}, got {capability!r}"
        )
    if ttl_days is not None and ttl_days < 0:
        raise ValueError(f"ttl_days must be >= 0 (0 means never expire), got {ttl_days}")
    now = store._clock() if isinstance(store, SQLitePatStore) else datetime.now(UTC)
    expires_at = now + timedelta(days=ttl_days) if ttl_days else None
    plaintext = secrets.token_urlsafe(32)
    digest = hash_pat(plaintext, pepper)
    if isinstance(store, SQLitePatStore):
        store.issue(friend_login, digest, expires_at=expires_at, capability=capability)
        return plaintext
    # In-memory mirror of SQLitePatStore.issue: store the namespaced principal and
    # revoke prior active rows matching it (including a legacy bare-login row).
    principal = normalize_principal(friend_login)
    values = set(principal_match_values(principal))
    for record in store.records:
        if record.friend_login in values and record.revoked_at is None:
            record.revoked_at = now
    store.records.append(
        PatRecord(
            friend_login=principal,
            pat_hash=digest,
            created_at=now,
            expires_at=expires_at,
            capability=capability,
        )
    )
    return plaintext


def verify_pat(
    store: InMemoryPatStore | SQLitePatStore,
    plaintext: str,
    pepper: str,
) -> tuple[str, Capability] | None:
    """Returns (login, capability) on success; None on every failure path.

    Failure-path uniformity: on the in-memory path, a dummy hmac.compare_digest runs
    when no record matches so short-circuit timing does not distinguish "unknown
    token" from "valid token". On the SQLite path, the SELECT is unconditional on
    pat_hash; expires_at, capability, and revoked_at are evaluated in Python after
    fetch so the query plan does not differ between failure causes.
    """
    digest = hash_pat(plaintext, pepper)
    if isinstance(store, SQLitePatStore):
        return store.verify(digest)
    now = datetime.now(UTC)
    for record in store.records:
        if not hmac.compare_digest(record.pat_hash, digest):
            continue
        if record.revoked_at is not None:
            return None
        if record.expires_at is not None and record.expires_at <= now:
            return None
        try:
            principal = normalize_principal(record.friend_login)
        except ValueError:
            return None
        record.last_used_at = now
        return principal, record.capability
    # Keep one compare on the failure path so obviously short-circuit timing is avoided.
    hmac.compare_digest(digest, "0" * 64)
    return None


def list_pats(store: SQLitePatStore) -> list[PatRecord]:
    """Operator-facing PAT inventory for `meme-mcp pat list`. Not timing-sensitive —
    runs from the CLI, never from a request handler. Ordering: active before revoked;
    within each group, soonest-expiring first (NULL expiries sort last so "never
    expires" reads as the lowest-priority warning).
    """
    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT friend_login, pat_hash, created_at, expires_at, capability,
                   revoked_at, last_used_at
            FROM pats
            ORDER BY revoked_at IS NOT NULL, expires_at IS NULL, expires_at, created_at
            """
        ).fetchall()
    return [
        PatRecord(
            friend_login=str(login),
            pat_hash=str(pat_hash),
            created_at=datetime.fromisoformat(str(created_at)),
            expires_at=_parse_optional_iso(expires_at),
            capability=_coerce_capability(capability) or DEFAULT_CAPABILITY,
            revoked_at=_parse_optional_iso(revoked_at),
            last_used_at=_parse_optional_iso(last_used_at),
        )
        for login, pat_hash, created_at, expires_at, capability, revoked_at, last_used_at in rows
    ]


def expires_at_for_login(store: SQLitePatStore, login: str) -> datetime | None:
    """Returns the soonest-expiring active PAT's expires_at for the given login, or
    None when no active PAT exists or the active PAT has no expiry. Used by the web
    expiry banner; PAT-authenticated requests can surface a "renew soon" warning
    without paying a second pat_hash lookup.
    """
    placeholders, values = principal_in_clause(login)
    with store._connect() as conn:
        row = conn.execute(
            f"""
            SELECT expires_at FROM pats
            WHERE friend_login IN ({placeholders}) AND revoked_at IS NULL
            AND expires_at IS NOT NULL
            ORDER BY expires_at ASC LIMIT 1
            """,
            values,
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return _parse_optional_iso(row[0])


def _parse_optional_iso(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _is_expired(expires_raw: object, now: datetime) -> bool:
    """Fail-closed expiry check on a raw expires_at column value.

    NULL means "never expires" (not expired). Any non-null value that fails to
    parse, is naive, or is at/before ``now`` reads as expired, matching verify's
    parse-before-compare discipline on the same raw value (SEC-001).
    """
    if expires_raw is None:
        return False
    expires_at = _parse_optional_iso(expires_raw)
    return expires_at is None or expires_at.tzinfo is None or expires_at <= now


def _coerce_capability(value: object) -> Capability | None:
    """Returns the matching Capability, DEFAULT_CAPABILITY for NULL (legacy rows),
    or None for any other value so the caller fails closed on corrupt data.
    """
    if value is None:
        return DEFAULT_CAPABILITY
    if value == "read":
        return "read"
    if value == "readwrite":
        return "readwrite"
    return None


def _timing_safe_compare(pat_hash: str) -> None:
    """One constant-time compare on every SQLite verify failure path so unknown /
    revoked / expired / corrupt branches cost the same work as a successful hash check.
    """
    hmac.compare_digest(pat_hash, "0" * 64)
