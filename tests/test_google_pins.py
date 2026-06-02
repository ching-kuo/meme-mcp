from __future__ import annotations

from pathlib import Path

from meme_mcp.auth.google_pins import SQLiteGooglePinStore


def _store(tmp_path: Path) -> SQLiteGooglePinStore:
    return SQLiteGooglePinStore(tmp_path / "pins.db")


def test_create_pin_first_time_binds_sub_to_email(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.create_pin("sub-A", "alice@gmail.com") is True
    assert store.email_for_sub("sub-A") == "alice@gmail.com"
    assert store.sub_for_email("alice@gmail.com") == "sub-A"


def test_create_pin_is_idempotent_for_same_sub_and_email(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.create_pin("sub-A", "alice@gmail.com") is True
    assert store.create_pin("sub-A", "alice@gmail.com") is True


def test_first_sign_in_wins_rejects_second_sub_for_same_email(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.create_pin("sub-A", "alice@gmail.com") is True
    # A different sub presenting the already-pinned email is rejected (email UNIQUE).
    assert store.create_pin("sub-B", "alice@gmail.com") is False
    # The real owner's pin is unchanged.
    assert store.sub_for_email("alice@gmail.com") == "sub-A"


def test_pin_email_is_immutable_for_a_sub(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.create_pin("sub-A", "alice@gmail.com") is True
    # The same sub cannot repin to a different mailbox.
    assert store.create_pin("sub-A", "other@gmail.com") is False
    assert store.email_for_sub("sub-A") == "alice@gmail.com"


def test_delete_is_terminal_and_allows_fresh_pin(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_pin("sub-A", "alice@gmail.com")
    assert store.delete_by_email("alice@gmail.com") is True
    assert store.email_for_sub("sub-A") is None
    # Re-invite + fresh sign-in: a new pin can be created (possibly a new sub).
    assert store.create_pin("sub-C", "alice@gmail.com") is True
    assert store.sub_for_email("alice@gmail.com") == "sub-C"


def test_delete_by_sub(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_pin("sub-A", "alice@gmail.com")
    assert store.delete_by_sub("sub-A") is True
    assert store.email_for_sub("sub-A") is None
    assert store.delete_by_sub("sub-A") is False


def test_all_pins_lists_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_pin("sub-A", "alice@gmail.com")
    store.create_pin("sub-B", "bob@gmail.com")
    pins = store.all_pins()
    assert {(sub, email) for sub, email, _ in pins} == {
        ("sub-A", "alice@gmail.com"),
        ("sub-B", "bob@gmail.com"),
    }
