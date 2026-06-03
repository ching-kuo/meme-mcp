from __future__ import annotations

from pathlib import Path

from meme_mcp.auth.allowlist import FileAllowlist


def _allowlist(tmp_path: Path, *lines: str) -> FileAllowlist:
    path = tmp_path / "allowlist.txt"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return FileAllowlist(path)


def test_bare_and_namespaced_github_entries_are_equivalent(tmp_path: Path) -> None:
    allow = _allowlist(tmp_path, "bob", "github:carol")
    # is_authorized passes the bare login for GitHub principals.
    assert allow.is_allowlisted("bob") is True
    assert allow.is_allowlisted("carol") is True
    # The github: prefixed query form also resolves.
    assert allow.is_allowlisted("github:bob") is True
    assert allow.is_allowlisted("dave") is False


def test_ae3_provider_scoped_matching(tmp_path: Path) -> None:
    # Bare bob (GitHub) and namespaced Google carol; cross-provider never collides.
    allow = _allowlist(tmp_path, "bob", "google:carol@gmail.com")
    assert allow.is_allowlisted("bob") is True
    assert allow.is_allowlisted("google:carol@gmail.com") is True
    # A GitHub login that happens to look like the Google email does NOT match the
    # google: entry (provider-scoped), and an absent Google user is rejected.
    assert allow.is_allowlisted("github:carol@gmail.com") is False
    assert allow.is_allowlisted("google:dave@gmail.com") is False


def test_google_email_never_matches_a_bare_github_entry(tmp_path: Path) -> None:
    allow = _allowlist(tmp_path, "alice@gmail.com")  # bare => GitHub-scoped
    # The bare entry is a (pathological) GitHub login, not a Google mailbox.
    assert allow.is_allowlisted("google:alice@gmail.com") is False


def test_mixed_case_and_comments_handled(tmp_path: Path) -> None:
    allow = _allowlist(tmp_path, "# a comment", "GitHub:Bob", "", "google:Carol@Gmail.com")
    assert allow.is_allowlisted("bob") is True
    assert allow.is_allowlisted("google:carol@gmail.com") is True
    # The list view preserves the namespaced prefix (lowercased) and drops comments.
    assert allow.entries() == ["github:bob", "google:carol@gmail.com"]


def test_r16_gmail_dot_and_plus_canonicalization(tmp_path: Path) -> None:
    allow = _allowlist(tmp_path, "google:alice@gmail.com")
    # Dot and +suffix variants of the same Gmail mailbox all match the invite.
    assert allow.is_allowlisted("google:a.l.i.c.e@gmail.com") is True
    assert allow.is_allowlisted("google:alice+meme@gmail.com") is True
    assert allow.is_allowlisted("google:ALICE@gmail.com") is True
    # A genuinely different mailbox does not match.
    assert allow.is_allowlisted("google:alicee@gmail.com") is False


def test_r16_canonicalization_is_symmetric_on_the_stored_entry(tmp_path: Path) -> None:
    # The operator may type the dotted/suffixed form; it still matches the plain claim.
    allow = _allowlist(tmp_path, "google:a.lice+x@googlemail.com")
    assert allow.is_allowlisted("google:alice@googlemail.com") is True


def test_google_alias_invite_is_removable_by_canonical_form(tmp_path: Path) -> None:
    # Regression: an alias invite must be stored canonically so a de-invite by the
    # plain mailbox actually removes it (otherwise the invite silently survives and
    # keeps authorizing the account after removal).
    allow = FileAllowlist(tmp_path / "allowlist.txt")
    allow.add("google:a.l.i.c.e+foo@gmail.com")
    assert allow.is_allowlisted("google:alice@gmail.com") is True
    allow.remove("google:alice@gmail.com")
    assert allow.is_allowlisted("google:alice@gmail.com") is False
    assert allow.entries() == []


def test_add_and_remove_roundtrip(tmp_path: Path) -> None:
    allow = FileAllowlist(tmp_path / "allowlist.txt")
    allow.add("google:Friend@Gmail.com")
    allow.add("bob")
    assert allow.is_allowlisted("bob") is True
    assert allow.is_allowlisted("google:friend@gmail.com") is True
    allow.remove("bob")
    assert allow.is_allowlisted("bob") is False
