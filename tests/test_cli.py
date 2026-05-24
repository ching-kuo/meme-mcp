from __future__ import annotations

from pydantic import SecretStr

from meme_mcp.__main__ import run
from meme_mcp.auth.pat import SQLitePatStore, verify_pat
from meme_mcp.config import Settings


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
    assert verify_pat(store, token, app_settings.pat_hash_pepper.get_secret_value()) == "friend"
