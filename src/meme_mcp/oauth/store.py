"""SQLite persistence + token primitives for the MCP OAuth authorization server.

Mirrors the established auth-store substrate (``SQLitePatStore``,
``SQLiteGooglePinStore``): a local SQLite file regardless of ``DATABASE_URL``
dialect, raw ``sqlite3`` (not SQLAlchemy async), self-creating its tables as
defense-in-depth with an Alembic migration (``0004_oauth``) mirroring the shape.

Security disciplines carried over from ``pat.py``:

* Access tokens, refresh tokens, authorization codes, and the consent-flow nonce
  are stored **one-way HMAC-hashed** with a dedicated ``oauth_token_pepper``;
  plaintext is never persisted or logged.
* Verification reads are unconditional hash lookups; expiry / revoked / state
  checks run in Python after fetch and fail closed on malformed data, so the
  query plan does not distinguish "unknown" from "revoked" from "expired".

The one exception (F-001): a client secret minted for a confidential client is
stored **encrypted** (reversible AEAD via ``OAUTH_SECRET_ENC_KEY``), because the
SDK's stock ``ClientAuthenticator`` compares the presented secret directly to the
value ``get_client()`` returns — a one-way hash could never satisfy that compare.
Public (PKCE-only) clients store no secret at all.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from mcp.shared.auth import OAuthClientInformationFull

# Token lifetimes. Access tokens are deliberately short (rotation + reuse
# detection govern the long-lived grant); the authorization code is single-use
# and short; the pending-request record must outlive an interactive login.
ACCESS_TTL_SECONDS = 15 * 60
REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60
AUTH_CODE_TTL_SECONDS = 5 * 60
PENDING_REQUEST_TTL_SECONDS = 10 * 60

# Idempotent-rotation grace (R9): re-presenting the immediately-prior refresh
# token within this window mints a fresh pair WITHOUT tripping family revocation,
# so a dropped /token response over a flaky network does not log the friend out.
# Beyond the window, re-presenting a rotated-away token is treated as reuse and
# revokes the whole family. (Byte-identical re-issue is impossible under
# hash-at-rest, so the grace window suppresses revocation rather than returning
# the original successor's plaintext — same security outcome, no logout.)
REFRESH_GRACE_SECONDS = 30
_REFRESH_GRACE = timedelta(seconds=REFRESH_GRACE_SECONDS)

RefreshState = Literal["active", "grace"]


@dataclass(frozen=True)
class StoredAuthCode:
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: tuple[str, ...]
    principal: str
    resource: str | None
    expires_at: datetime


@dataclass(frozen=True)
class StoredAccessToken:
    client_id: str
    principal: str
    scopes: tuple[str, ...]
    resource: str | None
    expires_at: datetime
    family_id: str


@dataclass(frozen=True)
class StoredRefreshToken:
    token: str
    client_id: str
    principal: str
    scopes: tuple[str, ...]
    resource: str | None
    family_id: str
    expires_at: datetime | None
    state: RefreshState


@dataclass(frozen=True)
class PendingRequest:
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: tuple[str, ...]
    resource: str | None
    state: str | None


def generate_token() -> str:
    """A fresh opaque token / code / nonce (>=160 bits, per RFC 6749 §10.10)."""
    return secrets.token_urlsafe(32)


def _join_scopes(scopes: list[str] | tuple[str, ...]) -> str:
    return " ".join(scopes)


def _split_scopes(raw: object) -> tuple[str, ...]:
    return tuple(s for s in str(raw or "").split(" ") if s)


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO timestamp, returning None on NULL or malformed input."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _expired(value: object, now: datetime) -> bool:
    """Fail-closed expiry on a raw ISO column (NULL = never expires)."""
    if value is None:
        return False
    parsed = _parse_dt(value)
    return parsed is None or parsed.tzinfo is None or parsed <= now


class SQLiteOAuthStore:
    def __init__(
        self,
        path: str | Path,
        *,
        token_pepper: str,
        secret_enc_key: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._pepper = token_pepper
        # Derive a valid Fernet key from the operator-provided enc key so the key
        # need not itself be base64/32 bytes (it is validated for length/strength
        # in config). Fernet provides authenticated encryption (AES-CBC + HMAC).
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(secret_enc_key.encode()).digest())
        self._fernet = Fernet(derived_key)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for ddl in _TABLE_DDL:
                conn.execute(ddl)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _hash(self, plaintext: str) -> str:
        return hmac.new(self._pepper.encode(), plaintext.encode(), "sha256").hexdigest()

    # -- client secret encryption (reversible; confidential clients only) -------

    def encrypt_secret(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt_secret(self, ciphertext: str) -> str | None:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            return None

    # -- clients ----------------------------------------------------------------

    def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a registered client. The plaintext ``client_secret`` (if the
        SDK minted one for a confidential client) is stored encrypted in its own
        column and stripped from the serialized metadata payload."""
        now = self._clock().isoformat()
        secret = client_info.client_secret
        encrypted = self.encrypt_secret(secret) if secret else None
        # Serialize the full metadata with the secret nulled — the secret lives
        # only in the encrypted column, never in the JSON payload.
        payload = client_info.model_copy(update={"client_secret": None})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO oauth_clients
                    (client_id, client_info, client_secret_encrypted,
                     client_secret_expires_at, registered_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (
                    client_info.client_id,
                    payload.model_dump_json(),
                    encrypted,
                    client_info.client_secret_expires_at,
                    now,
                ),
            )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_info, client_secret_encrypted FROM oauth_clients "
                "WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        client = OAuthClientInformationFull.model_validate_json(str(row[0]))
        if row[1] is not None:
            # Return the decrypted plaintext so the SDK ClientAuthenticator can
            # compare it directly against the presented secret (F-001).
            client.client_secret = self.decrypt_secret(str(row[1]))
        return client

    def mark_client_used(self, client_id: str) -> None:
        """Stamp the last successful authorization (drives unused-client GC, U6)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?",
                (self._clock().isoformat(), client_id),
            )

    # -- per-(principal, client) consent approvals ------------------------------

    def record_approval(self, principal: str, client_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO oauth_client_approvals (principal, client_id, approved_at)
                VALUES (?, ?, ?)
                """,
                (principal, client_id, self._clock().isoformat()),
            )

    def has_approval(self, principal: str, client_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM oauth_client_approvals WHERE principal = ? AND client_id = ?",
                (principal, client_id),
            ).fetchone()
        return row is not None

    def delete_approvals_for_principal(self, principal: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM oauth_client_approvals WHERE principal = ?", (principal,)
            )
        return cursor.rowcount

    # -- pending authorization requests (nonce-keyed; U4) -----------------------

    def create_pending_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        scopes: list[str],
        resource: str | None,
        state: str | None,
    ) -> str:
        """Persist the validated /authorize params and return a single-use nonce.

        The provider's ``authorize()`` hook receives no ``Request`` and cannot
        touch the session, so authorization state is parked here keyed by a nonce
        carried in the redirect to the consent route (KTD6/F-002). Surviving in
        the store (not the session) means it outlives the ``session.clear()`` the
        login callbacks perform."""
        nonce = generate_token()
        now = self._clock()
        expires = now + timedelta(seconds=PENDING_REQUEST_TTL_SECONDS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO oauth_pending_requests
                    (nonce_hash, client_id, redirect_uri, redirect_uri_provided_explicitly,
                     code_challenge, scopes, resource, state, created_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    self._hash(nonce),
                    client_id,
                    redirect_uri,
                    int(redirect_uri_provided_explicitly),
                    code_challenge,
                    _join_scopes(scopes),
                    resource,
                    state,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
        return nonce

    def load_pending_request(self, nonce: str) -> PendingRequest | None:
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT client_id, redirect_uri, redirect_uri_provided_explicitly,
                       code_challenge, scopes, resource, state, expires_at, consumed_at
                FROM oauth_pending_requests WHERE nonce_hash = ?
                """,
                (self._hash(nonce),),
            ).fetchone()
        if row is None or row[8] is not None or _expired(row[7], now):
            return None
        return PendingRequest(
            client_id=str(row[0]),
            redirect_uri=str(row[1]),
            redirect_uri_provided_explicitly=bool(row[2]),
            code_challenge=str(row[3]),
            scopes=_split_scopes(row[4]),
            resource=None if row[5] is None else str(row[5]),
            state=None if row[6] is None else str(row[6]),
        )

    def consume_pending_request(self, nonce: str) -> bool:
        """Atomically mark the pending request consumed; returns False if it was
        already consumed (so a replayed consent POST cannot reissue a code)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE oauth_pending_requests SET consumed_at = ? "
                "WHERE nonce_hash = ? AND consumed_at IS NULL",
                (self._clock().isoformat(), self._hash(nonce)),
            )
        return cursor.rowcount > 0

    # -- authorization codes ----------------------------------------------------

    def create_auth_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        scopes: list[str],
        principal: str,
        resource: str | None,
    ) -> str:
        code = generate_token()
        now = self._clock()
        expires = now + timedelta(seconds=AUTH_CODE_TTL_SECONDS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO oauth_auth_codes
                    (code_hash, client_id, redirect_uri, redirect_uri_provided_explicitly,
                     code_challenge, scopes, principal, resource, expires_at, created_at,
                     consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    self._hash(code),
                    client_id,
                    redirect_uri,
                    int(redirect_uri_provided_explicitly),
                    code_challenge,
                    _join_scopes(scopes),
                    principal,
                    resource,
                    expires.isoformat(),
                    now.isoformat(),
                ),
            )
        return code

    def load_auth_code(self, code: str) -> StoredAuthCode | None:
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT client_id, redirect_uri, redirect_uri_provided_explicitly,
                       code_challenge, scopes, principal, resource, expires_at, consumed_at
                FROM oauth_auth_codes WHERE code_hash = ?
                """,
                (self._hash(code),),
            ).fetchone()
        if row is None or row[8] is not None or _expired(row[7], now):
            return None
        expires = _parse_dt(row[7])
        if expires is None:
            return None
        return StoredAuthCode(
            client_id=str(row[0]),
            redirect_uri=str(row[1]),
            redirect_uri_provided_explicitly=bool(row[2]),
            code_challenge=str(row[3]),
            scopes=_split_scopes(row[4]),
            principal=str(row[5]),
            resource=None if row[6] is None else str(row[6]),
            expires_at=expires,
        )

    def consume_auth_code(self, code: str) -> bool:
        """Atomically mark a code consumed; returns False if already consumed
        (single-use enforcement, R10)."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE oauth_auth_codes SET consumed_at = ? "
                "WHERE code_hash = ? AND consumed_at IS NULL",
                (self._clock().isoformat(), self._hash(code)),
            )
        return cursor.rowcount > 0

    # -- tokens -----------------------------------------------------------------

    def issue_initial_tokens(
        self,
        *,
        client_id: str,
        principal: str,
        scopes: list[str],
        resource: str | None,
    ) -> tuple[str, str]:
        """Mint the first (access, refresh) pair for a freshly-redeemed code,
        opening a new rotation family. Returns the plaintext pair."""
        family_id = generate_token()
        with self._connect() as conn:
            access = self._mint_access(conn, family_id, client_id, principal, scopes, resource)
            refresh = self._mint_refresh(
                conn, family_id, None, client_id, principal, scopes, resource
            )
        return access, refresh

    def _mint_access(
        self,
        conn: sqlite3.Connection,
        family_id: str,
        client_id: str,
        principal: str,
        scopes: list[str] | tuple[str, ...],
        resource: str | None,
    ) -> str:
        token = generate_token()
        now = self._clock()
        expires = now + timedelta(seconds=ACCESS_TTL_SECONDS)
        conn.execute(
            """
            INSERT INTO oauth_access_tokens
                (token_hash, family_id, client_id, principal, scopes, resource,
                 expires_at, created_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                self._hash(token),
                family_id,
                client_id,
                principal,
                _join_scopes(scopes),
                resource,
                expires.isoformat(),
                now.isoformat(),
            ),
        )
        return token

    def _mint_refresh(
        self,
        conn: sqlite3.Connection,
        family_id: str,
        prev_token_hash: str | None,
        client_id: str,
        principal: str,
        scopes: list[str] | tuple[str, ...],
        resource: str | None,
    ) -> str:
        token = generate_token()
        now = self._clock()
        expires = now + timedelta(seconds=REFRESH_TTL_SECONDS)
        conn.execute(
            """
            INSERT INTO oauth_refresh_tokens
                (token_hash, family_id, prev_token_hash, client_id, principal, scopes,
                 resource, expires_at, created_at, rotated_at, revoked_at, superseded_by_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (
                self._hash(token),
                family_id,
                prev_token_hash,
                client_id,
                principal,
                _join_scopes(scopes),
                resource,
                expires.isoformat(),
                now.isoformat(),
            ),
        )
        return token

    def load_access_token(self, token: str) -> StoredAccessToken | None:
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT client_id, principal, scopes, resource, expires_at, family_id, revoked_at
                FROM oauth_access_tokens WHERE token_hash = ?
                """,
                (self._hash(token),),
            ).fetchone()
        if row is None or row[6] is not None or _expired(row[4], now):
            return None
        expires = _parse_dt(row[4])
        if expires is None:
            return None
        return StoredAccessToken(
            client_id=str(row[0]),
            principal=str(row[1]),
            scopes=_split_scopes(row[2]),
            resource=None if row[3] is None else str(row[3]),
            expires_at=expires,
            family_id=str(row[5]),
        )

    def load_refresh_token(self, token: str) -> StoredRefreshToken | None:
        """Load a refresh token for use, applying reuse detection.

        Returns the token with ``state="active"`` (never rotated) or
        ``state="grace"`` (rotated within the grace window — a legitimate retry).
        Returns None for unknown / revoked / expired tokens AND for a token
        rotated away beyond the grace window — the latter is reuse, so the whole
        family is revoked as a side effect (R9)."""
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT client_id, principal, scopes, resource, family_id,
                       expires_at, rotated_at, revoked_at
                FROM oauth_refresh_tokens WHERE token_hash = ?
                """,
                (self._hash(token),),
            ).fetchone()
            if row is None or row[7] is not None or _expired(row[5], now):
                return None
            rotated_at = _parse_dt(row[6])
            if row[6] is not None and rotated_at is None:
                return None  # malformed rotated_at: fail closed
            state: RefreshState
            if rotated_at is None:
                state = "active"
            elif (now - rotated_at) <= _REFRESH_GRACE:
                state = "grace"
            else:
                self._revoke_family(conn, str(row[4]))
                return None
        return StoredRefreshToken(
            token=token,
            client_id=str(row[0]),
            principal=str(row[1]),
            scopes=_split_scopes(row[2]),
            resource=None if row[3] is None else str(row[3]),
            family_id=str(row[4]),
            expires_at=_parse_dt(row[5]),
            state=state,
        )

    def rotate_refresh_token(self, token: str, scopes: list[str]) -> tuple[str, str] | None:
        """Rotate a refresh token: mint a new (access, refresh) pair in the same
        family. An active token is marked rotated and linked to its successor; a
        grace-window token mints a fresh pair without revoking (idempotent retry).
        Returns the plaintext pair, or None when the token is not rotatable."""
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT client_id, principal, scopes, resource, family_id,
                       expires_at, rotated_at, revoked_at
                FROM oauth_refresh_tokens WHERE token_hash = ?
                """,
                (self._hash(token),),
            ).fetchone()
            if row is None or row[7] is not None or _expired(row[5], now):
                return None
            rotated_at = _parse_dt(row[6])
            if row[6] is not None and rotated_at is None:
                return None
            if rotated_at is not None and (now - rotated_at) > _REFRESH_GRACE:
                self._revoke_family(conn, str(row[4]))
                return None
            client_id, principal, family_id = str(row[0]), str(row[1]), str(row[4])
            resource = None if row[3] is None else str(row[3])
            # Narrow to the intersection of granted and requested scopes (the SDK
            # handler already rejected scopes outside the grant).
            granted = _split_scopes(row[2])
            new_scopes = [s for s in scopes if s in granted] if scopes else list(granted)
            digest = self._hash(token)
            access = self._mint_access(conn, family_id, client_id, principal, new_scopes, resource)
            refresh = self._mint_refresh(
                conn, family_id, digest, client_id, principal, new_scopes, resource
            )
            if rotated_at is None:
                conn.execute(
                    "UPDATE oauth_refresh_tokens SET rotated_at = ?, superseded_by_hash = ? "
                    "WHERE token_hash = ?",
                    (now.isoformat(), self._hash(refresh), digest),
                )
        return access, refresh

    # -- revocation -------------------------------------------------------------

    def revoke_token(self, token: str) -> None:
        """Revoke the token and its whole grant family, whether an access or a
        refresh token is presented (RFC 7009 / SDK contract)."""
        digest = self._hash(token)
        with self._connect() as conn:
            family = self._family_for(conn, digest)
            if family is not None:
                self._revoke_family(conn, family)

    def revoke_families_for_principal(self, principal: str) -> int:
        """Revoke every active grant for a principal (allowlist removal, U6).
        Returns the number of refresh-token families touched."""
        now = self._clock().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE oauth_refresh_tokens SET revoked_at = ? "
                "WHERE principal = ? AND revoked_at IS NULL",
                (now, principal),
            )
            conn.execute(
                "UPDATE oauth_access_tokens SET revoked_at = ? "
                "WHERE principal = ? AND revoked_at IS NULL",
                (now, principal),
            )
        return cursor.rowcount

    def _family_for(self, conn: sqlite3.Connection, token_hash: str) -> str | None:
        for table in ("oauth_access_tokens", "oauth_refresh_tokens"):
            row = conn.execute(
                f"SELECT family_id FROM {table} WHERE token_hash = ?", (token_hash,)  # noqa: S608 - fixed table names
            ).fetchone()
            if row is not None:
                return str(row[0])
        return None

    def _revoke_family(self, conn: sqlite3.Connection, family_id: str) -> None:
        now = self._clock().isoformat()
        conn.execute(
            "UPDATE oauth_access_tokens SET revoked_at = ? "
            "WHERE family_id = ? AND revoked_at IS NULL",
            (now, family_id),
        )
        conn.execute(
            "UPDATE oauth_refresh_tokens SET revoked_at = ? "
            "WHERE family_id = ? AND revoked_at IS NULL",
            (now, family_id),
        )

    # -- garbage collection (U6) ------------------------------------------------

    def gc_expired_tokens(self) -> int:
        """Delete expired or revoked access/refresh rows. Returns rows removed."""
        now = self._clock().isoformat()
        removed = 0
        with self._connect() as conn:
            for table in ("oauth_access_tokens", "oauth_refresh_tokens"):
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE revoked_at IS NOT NULL "  # noqa: S608 - fixed table names
                    f"OR (expires_at IS NOT NULL AND expires_at <= ?)",
                    (now,),
                )
                removed += cursor.rowcount
        return removed

    def gc_unused_clients(self, ttl_days: int) -> int:
        """Delete clients with no successful authorization within ``ttl_days``
        (a registered-but-never-used client is open-DCR storage cruft)."""
        cutoff = (self._clock() - timedelta(days=ttl_days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM oauth_clients WHERE "
                "(last_used_at IS NULL AND registered_at <= ?) OR last_used_at <= ?",
                (cutoff, cutoff),
            )
        return cursor.rowcount


_TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id TEXT PRIMARY KEY,
        client_info TEXT NOT NULL,
        client_secret_encrypted TEXT,
        client_secret_expires_at INTEGER,
        registered_at TEXT NOT NULL,
        last_used_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_auth_codes (
        code_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        redirect_uri_provided_explicitly INTEGER NOT NULL,
        code_challenge TEXT NOT NULL,
        scopes TEXT NOT NULL,
        principal TEXT NOT NULL,
        resource TEXT,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
        token_hash TEXT PRIMARY KEY,
        family_id TEXT NOT NULL,
        prev_token_hash TEXT,
        client_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        scopes TEXT NOT NULL,
        resource TEXT,
        expires_at TEXT,
        created_at TEXT NOT NULL,
        rotated_at TEXT,
        revoked_at TEXT,
        superseded_by_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_access_tokens (
        token_hash TEXT PRIMARY KEY,
        family_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        principal TEXT NOT NULL,
        scopes TEXT NOT NULL,
        resource TEXT,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_client_approvals (
        principal TEXT NOT NULL,
        client_id TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        PRIMARY KEY (principal, client_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oauth_pending_requests (
        nonce_hash TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        redirect_uri TEXT NOT NULL,
        redirect_uri_provided_explicitly INTEGER NOT NULL,
        code_challenge TEXT NOT NULL,
        scopes TEXT NOT NULL,
        resource TEXT,
        state TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
)
