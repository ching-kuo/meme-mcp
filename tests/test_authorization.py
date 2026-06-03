from __future__ import annotations

from pathlib import Path

import pytest

from meme_mcp.auth.authorization import (
    display_label,
    is_authorized,
    normalize_principal,
    principal_match_values,
)
from meme_mcp.auth.depends import Friend, require_operator
from meme_mcp.errors import MemeMCPError


class _StubAllowlist:
    def __init__(self, *values: str) -> None:
        self._values = set(values)

    def is_allowlisted(self, value: str) -> bool:
        return value in self._values


# --- normalize_principal (R14) -------------------------------------------------


def test_bare_github_login_gets_default_prefix() -> None:
    assert normalize_principal("alice") == "github:alice"
    assert normalize_principal("a-b_c") == "github:a-b_c"


def test_already_namespaced_is_returned_unchanged_and_idempotent() -> None:
    assert normalize_principal("github:alice") == "github:alice"
    assert normalize_principal("google:11769") == "google:11769"
    # Idempotent: normalizing twice never re-prefixes.
    assert normalize_principal(normalize_principal("alice")) == "github:alice"


def test_bare_value_with_at_is_rejected_not_minted_into_github() -> None:
    # An email-shaped bare value must never become github:<email> (R14).
    with pytest.raises(ValueError):
        normalize_principal("alice@gmail.com")


def test_bare_value_with_colon_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_principal("evil:payload")
    with pytest.raises(ValueError):
        normalize_principal("github:")  # empty subject


def test_empty_or_blank_rejected() -> None:
    for bad in ("", "   ", "\t"):
        with pytest.raises(ValueError):
            normalize_principal(bad)


# --- principal_match_values ----------------------------------------------------


def test_match_values_github_includes_bare_legacy_form() -> None:
    assert set(principal_match_values("github:alice")) == {"github:alice", "alice"}
    # Accepts a bare value too (used by direct-store callers in tests).
    assert set(principal_match_values("alice")) == {"github:alice", "alice"}


def test_match_values_google_has_no_bare_form() -> None:
    assert principal_match_values("google:11769") == ("google:11769",)


# --- display_label -------------------------------------------------------------


def test_display_label_strips_github_prefix() -> None:
    assert display_label("github:alice") == "alice"


def test_display_label_resolves_google_sub_to_pinned_mailbox() -> None:
    pins = _StubPinStore(sub11769="alice@gmail.com")
    # Never the raw google:<sub>; resolves to the pinned mailbox.
    assert display_label("google:sub11769", pins) == "alice@gmail.com"


# --- is_authorized -------------------------------------------------------------


def test_is_authorized_github_consults_allowlist_with_bare_login() -> None:
    allow = _StubAllowlist("alice")
    assert is_authorized("github:alice", allowlist=allow) is True
    assert is_authorized("github:stranger", allowlist=allow) is False


def test_is_authorized_google_denied_without_pin_store() -> None:
    # Until a pin store is supplied a Google principal is never authorized.
    allow = _StubAllowlist("google:alice@gmail.com")
    assert is_authorized("google:11769", allowlist=allow) is False


class _StubPinStore:
    def __init__(self, **sub_to_email: str) -> None:
        self._pins = sub_to_email

    def email_for_sub(self, sub: str) -> str | None:
        return self._pins.get(sub)


def test_is_authorized_google_pinned_and_allowlisted() -> None:
    allow = _StubAllowlist("google:alice@gmail.com")
    pins = _StubPinStore(sub11769="alice@gmail.com")
    assert is_authorized("google:sub11769", allowlist=allow, pin_store=pins) is True


def test_is_authorized_google_pinned_but_not_allowlisted_denied() -> None:
    # Pin exists but the pinned mailbox is no longer allowlisted (de-invited).
    allow = _StubAllowlist()
    pins = _StubPinStore(sub11769="alice@gmail.com")
    assert is_authorized("google:sub11769", allowlist=allow, pin_store=pins) is False


def test_is_authorized_google_unpinned_sub_denied() -> None:
    allow = _StubAllowlist("google:alice@gmail.com")
    pins = _StubPinStore()  # no pin for this sub
    assert is_authorized("google:sub11769", allowlist=allow, pin_store=pins) is False


# --- require_operator (normalized comparison) ----------------------------------


def test_require_operator_matches_namespaced_principal() -> None:
    user = Friend("github:operator")
    assert require_operator(user, "operator") is user
    with pytest.raises(MemeMCPError):
        require_operator(Friend("github:someone"), "operator")


# --- grep-guard: the default prefix literal lives only in normalize_principal ---


def test_default_prefix_literal_only_in_normalize_principal() -> None:
    source = Path("src/meme_mcp/auth/authorization.py").read_text(encoding="utf-8")
    # The literal "github:" default prefix is applied in exactly one place. Other
    # references to the provider use the _DEFAULT_PROVIDER constant or comments.
    code_lines = [
        line
        for line in source.splitlines()
        if 'f"{_DEFAULT_PROVIDER}:' in line
    ]
    assert len(code_lines) == 1
