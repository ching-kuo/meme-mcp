from meme_mcp.auth.pat import InMemoryPatStore, issue_pat, verify_pat


def test_issue_and_verify_pat() -> None:
    store = InMemoryPatStore()
    plaintext = issue_pat(store, "alice", "pepper")
    assert len(plaintext) >= 40
    assert plaintext not in store.records[0].pat_hash
    assert verify_pat(store, plaintext, "pepper") == "alice"


def test_second_pat_revokes_first() -> None:
    store = InMemoryPatStore()
    first = issue_pat(store, "alice", "pepper")
    second = issue_pat(store, "alice", "pepper")
    assert verify_pat(store, first, "pepper") is None
    assert verify_pat(store, second, "pepper") == "alice"


def test_pepper_rotation_invalidates_existing_pat() -> None:
    store = InMemoryPatStore()
    token = issue_pat(store, "alice", "pepper")
    assert verify_pat(store, token, "new-pepper") is None

