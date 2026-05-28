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
