from __future__ import annotations

from meme_mcp.corpus.seed_memegen import SeedTemplate, seed_templates
from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.rendering.image_store import FilesystemImageStore


def test_seed_templates_persists_templates_and_images(tmp_path) -> None:
    repo = SQLiteTemplateRepository(tmp_path / "meme.db")
    image_store = FilesystemImageStore(tmp_path / "images")

    count = seed_templates(
        repo,
        image_store,
        [
            SeedTemplate(
                slug="drake",
                name="Drake Hotline Bling",
                slot_count=2,
                description="Reject one option and approve another.",
                tags=["choice", "reaction"],
            )
        ],
    )

    row = repo.get("memegen-drake")
    assert count == 1
    assert row.name == "Drake Hotline Bling"
    assert len(row.slot_definitions) == 2
    assert image_store.get(row.image_path).startswith(b"\x89PNG")


def test_seed_templates_is_idempotent(tmp_path) -> None:
    repo = SQLiteTemplateRepository(tmp_path / "meme.db")
    image_store = FilesystemImageStore(tmp_path / "images")
    templates = [SeedTemplate(slug="doge", name="Doge", slot_count=1)]

    assert seed_templates(repo, image_store, templates) == 1
    assert seed_templates(repo, image_store, templates) == 1

    assert len(repo.list_rows()) == 1
