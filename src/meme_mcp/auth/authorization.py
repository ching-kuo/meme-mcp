"""Provider-namespaced identity: the single authorization predicate and the
normalization shim, with no dependencies on the rest of the app.

This is a leaf module on purpose. ``session.py`` already imports ``require_pat``
from ``depends.py``, so housing the authorization predicate in either would
create an import cycle. A dependency-free leaf that both import *down* into is
the only placement that keeps the import graph acyclic while letting all three
front doors -- the browser session, the web/HTTP PAT, and the MCP transport PAT
-- share one authorization decision so they cannot diverge.

A principal is a ``provider:subject`` string:

* ``github:<login>`` -- the GitHub login (subject is the login).
* ``google:<sub>``   -- the immutable Google OIDC ``sub`` (the pinned mailbox the
  operator invited is resolved separately, in the pin store; see U6).

Legacy un-namespaced values (bare GitHub logins stored before this change in the
allowlist file, PAT rows, sessions, receipts, and pending uploads) are read as
``github:<value>`` so no data migration is needed.
"""

from __future__ import annotations

import re
from typing import Protocol

# Conservative bare-login charset. Its only job is to reject the dangerous
# shapes -- anything carrying ``@`` (an email masquerading as a GitHub login) or
# ``:`` (cross-provider / session-format confusion) -- not to validate GitHub
# registration. Underscore is permitted so ordinary identifiers are not rejected;
# a value failing this is never minted into a ``github:`` principal.
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# The only place the default ``github:`` prefix is applied. Keeping it a single
# named constant lets a regression test assert the literal appears nowhere else.
_DEFAULT_PROVIDER = "github"
KNOWN_PROVIDERS = ("github", "google")


class SupportsAllowlist(Protocol):
    def is_allowlisted(self, value: str) -> bool: ...


class SupportsPinLookup(Protocol):
    """The slice of the Google pin store the authorization predicate consults.

    Implemented in U6; declared here so ``is_authorized`` can type its argument
    without importing the concrete store (which would break the leaf's
    dependency-free contract).
    """

    def email_for_sub(self, sub: str) -> str | None: ...


def normalize_principal(value: str) -> str:
    """Return the canonical ``provider:subject`` principal for a raw value.

    Idempotent and prefix-preserving (R14): a value already carrying a known
    ``provider:`` prefix is returned unchanged, never re-prefixed. A legacy bare
    value becomes ``github:<value>`` only when it matches the conservative login
    charset; a bare value containing ``@`` or ``:`` (or any other out-of-charset
    character) is rejected rather than smuggled into a GitHub principal. This
    closes the cross-provider / session-format confusion seam.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate:
        raise ValueError("principal must be a non-empty string")
    provider, sep, subject = candidate.partition(":")
    if sep:
        # Already namespaced. Accept only a known provider with a non-empty
        # subject; never re-prefix.
        if provider not in KNOWN_PROVIDERS or not subject:
            raise ValueError(f"unknown or empty principal namespace: {value!r}")
        return candidate
    if not _GITHUB_LOGIN_RE.match(candidate):
        # Bare and out-of-charset (e.g. an email address): refuse to mint a
        # GitHub principal from it.
        raise ValueError(f"ambiguous bare principal: {value!r}")
    return f"{_DEFAULT_PROVIDER}:{candidate}"


def principal_match_values(value: str) -> tuple[str, ...]:
    """Stored values that should be treated as the same identity on read.

    Accepts either a namespaced principal or a legacy bare login, and returns the
    namespaced principal plus, for GitHub, the bare login, because pre-namespace
    rows (PATs, receipts, pending uploads) store the bare login. A lookup or
    reissue keyed on ``github:<login>`` must therefore also match a friend's
    pre-migration ``<login>`` rows so they keep working without a data migration.
    A value that cannot be normalized matches only itself (fail-closed).
    """
    try:
        principal = normalize_principal(value)
    except ValueError:
        return (value,)
    provider, _, subject = principal.partition(":")
    values = [principal]
    if value != principal:
        values.append(value)
    if provider == _DEFAULT_PROVIDER and subject and subject not in values:
        values.append(subject)
    return tuple(values)


def principal_in_clause(value: str) -> tuple[str, tuple[str, ...]]:
    """SQL ``IN`` placeholders + bind values for every stored form of a principal.

    Returns e.g. ``("?, ?", ("github:bob", "bob"))`` so a parameterized query can
    match the namespaced principal and its legacy bare row in one clause without
    each store hand-rolling the placeholder string.
    """
    values = principal_match_values(value)
    return ", ".join("?" * len(values)), values


def display_label(principal: str, pin_store: SupportsPinLookup | None = None) -> str:
    """Human-facing label for a principal.

    GitHub principals show the bare login (the ``github:`` prefix stripped).
    Google principals (``google:<sub>``) resolve to their pinned mailbox via the
    pin store, never the raw ``sub``. Falls back to the bare subject only when no
    pin is found (a logged-in Google friend always has a pin, so this is a
    defensive tail, not a normal path).
    """
    provider, _, subject = principal.partition(":")
    if provider == "google" and pin_store is not None:
        email = pin_store.email_for_sub(subject)
        if email is not None:
            return email
    return subject or principal


def is_authorized(
    principal: str,
    *,
    allowlist: SupportsAllowlist,
    pin_store: SupportsPinLookup | None = None,
) -> bool:
    """Whether ``principal`` is currently authorized, evaluated per request.

    Pure: takes the allowlist and (Google) pin store as arguments and reads no
    module/app state, so every caller gets the same decision against live state
    with no authorization caching (R12). The ``google:`` branch is completed in
    U6; until a pin store is supplied a Google principal is unauthorized.
    """
    provider, _, subject = principal.partition(":")
    if provider == _DEFAULT_PROVIDER:
        # Pass the bare login; FileAllowlist.is_allowlisted scopes a prefix-less
        # value to GitHub, matching both legacy bare and ``github:`` entries.
        return bool(subject) and allowlist.is_allowlisted(subject)
    if provider == "google":
        # A Google principal is google:<sub>. Resolve the immutable sub to its
        # pinned mailbox, then authorize that mailbox against the allowlist. A
        # missing pin store (Google off) or an unpinned sub is unauthorized. The
        # pinned email -- the operator's original invite -- is what is checked, so
        # a friend whose Gmail was renamed is still authorized (drift survives on
        # the sub).
        if pin_store is None or not subject:
            return False
        email = pin_store.email_for_sub(subject)
        if email is None:
            return False
        return allowlist.is_allowlisted(f"google:{email}")
    return False
