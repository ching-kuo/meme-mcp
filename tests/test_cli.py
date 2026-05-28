from __future__ import annotations

from pydantic import SecretStr

from meme_mcp.__main__ import run
from meme_mcp.auth.pat import SQLitePatStore, verify_pat
from meme_mcp.cli.reindex_embeddings import reindex_embeddings
from meme_mcp.config import Settings
from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.db.vectors import SQLiteVecStore


def settings(tmp_path) -> Settings:
    return Settings(
        storage_dir=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'meme.db'}",
        image_store_backend="filesystem",
        image_store_fs_path=str(tmp_path / "images"),
        github_client_id="cid",
        github_client_secret=SecretStr("secret-32-chars-value-for-tests"),
        github_redirect_uri="http://localhost:8000/auth/callback",
        github_allowlist_path=str(tmp_path / "allowlist.txt"),
        operator_github_login="operator",
        session_secret=SecretStr("session-secret-32-chars-value-tests"),
        pat_hash_pepper=SecretStr("pepper-secret-32-chars-value-tests"),
        vlm_base_url="https://example.test/v1",
        vlm_api_key=SecretStr("vlm-key"),
        vlm_model="vlm-model",
        embedding_api_key=SecretStr("embedding-key"),
    )


def test_allowlist_cli_add_list_remove(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)

    assert run(["allowlist", "add", "friend"], app_settings) == 0
    assert "friend" in (tmp_path / "allowlist.txt").read_text(encoding="utf-8")

    assert run(["allowlist", "list"], app_settings) == 0
    assert "friend" in capsys.readouterr().out

    assert run(["allowlist", "remove", "friend"], app_settings) == 0
    assert "friend" not in (tmp_path / "allowlist.txt").read_text(encoding="utf-8")


def test_pat_cli_issue_prints_verifiable_token(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)

    assert run(["pat", "issue", "friend"], app_settings) == 0
    token = capsys.readouterr().out.strip()

    store = SQLitePatStore(tmp_path / "meme.db")
    assert verify_pat(store, token, app_settings.pat_hash_pepper.get_secret_value()) == (
        "friend",
        "readwrite",
    )


def test_pat_cli_issue_respects_ttl_and_scope_flags(tmp_path, capsys) -> None:
    from meme_mcp.auth.pat import list_pats

    app_settings = settings(tmp_path)

    assert run(["pat", "issue", "friend", "--ttl-days", "30", "--scope", "read"], app_settings) == 0
    token = capsys.readouterr().out.strip()
    assert token

    store = SQLitePatStore(tmp_path / "meme.db")
    verified = verify_pat(store, token, app_settings.pat_hash_pepper.get_secret_value())
    assert verified == ("friend", "read")
    [record] = [r for r in list_pats(store) if r.revoked_at is None]
    assert record.expires_at is not None
    # ttl_days=30 with a small tolerance for the 1-2 second test latency.
    from datetime import UTC, datetime, timedelta

    delta = record.expires_at - datetime.now(UTC)
    assert timedelta(days=29, hours=23) <= delta <= timedelta(days=30, minutes=1)


def test_pat_cli_issue_ttl_zero_means_never_expires(tmp_path, capsys) -> None:
    from meme_mcp.auth.pat import list_pats

    app_settings = settings(tmp_path)
    assert run(["pat", "issue", "friend", "--ttl-days", "0"], app_settings) == 0
    capsys.readouterr()

    store = SQLitePatStore(tmp_path / "meme.db")
    [record] = [r for r in list_pats(store) if r.revoked_at is None]
    assert record.expires_at is None


def test_pat_cli_list_shows_active_and_revoked(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)
    # Empty case prints the no-PATs notice.
    assert run(["pat", "list"], app_settings) == 0
    assert "no PATs issued" in capsys.readouterr().out

    assert run(["pat", "issue", "alice"], app_settings) == 0
    capsys.readouterr()  # discard issued token
    assert run(["pat", "issue", "bob", "--scope", "read", "--ttl-days", "0"], app_settings) == 0
    capsys.readouterr()
    # Reissuing for alice revokes the prior PAT.
    assert run(["pat", "issue", "alice"], app_settings) == 0
    capsys.readouterr()

    assert run(["pat", "list"], app_settings) == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out
    assert "active" in out
    assert "revoked" in out
    assert "never" in out  # bob has no expiry
    assert "read" in out
    assert "readwrite" in out


def test_pat_cli_issue_rejects_invalid_scope(tmp_path) -> None:
    import pytest

    app_settings = settings(tmp_path)
    with pytest.raises(SystemExit):
        run(["pat", "issue", "friend", "--scope", "admin"], app_settings)


def test_pat_cli_issue_rejects_negative_ttl_with_clean_message(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)
    assert run(["pat", "issue", "friend", "--ttl-days", "-5"], app_settings) == 2
    out = capsys.readouterr().out
    assert "--ttl-days must be >= 0" in out


class FakeEmbeddingClient:
    def embed_template(self, metadata: dict[str, object]) -> list[float]:
        assert metadata["description"] == "ship green"
        return [1.0, 0.0, 0.0]


def test_reindex_embeddings_rebuilds_vector_store_from_templates(tmp_path) -> None:
    repo = SQLiteTemplateRepository(tmp_path / "meme.db")
    repo.upsert(
        TemplateCreate(
            template_id="deploy",
            slug="deploy",
            name="Deploy",
            source="friend",
            metadata={"description": "ship green", "tags": ["ci"]},
            slot_definitions=[{"position": "top"}],
            image_path="aa/deploy.png",
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )
    vectors = SQLiteVecStore(tmp_path / "vectors.db", dimensions=3)

    count = reindex_embeddings(repo, vectors, FakeEmbeddingClient())

    assert count == 1
    assert vectors.search([1.0, 0.0, 0.0], 1) == [("deploy", 1.0)]


def test_seed_memegen_cli_persists_default_templates(tmp_path, capsys) -> None:
    app_settings = settings(tmp_path)

    assert run(["seed-memegen"], app_settings) == 0

    assert "seeded" in capsys.readouterr().out
    repo = SQLiteTemplateRepository(tmp_path / "meme.db")
    assert repo.get("memegen-drake").name == "Drake Hotline Bling"
