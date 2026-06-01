from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from meme_mcp.corpus.upstream import (
    CANONICAL_POSITIONS,
    import_upstream_corpus,
    load_upstream_template,
    project_slot_position,
    slot_definitions,
)
from meme_mcp.db.templates import SQLiteTemplateRepository
from meme_mcp.rendering.image_store import FilesystemImageStore


def _drake_top_text() -> dict[str, float | str]:
    return {
        "anchor_x": 0.0,
        "anchor_y": 0.0,
        "scale_x": 1.0,
        "scale_y": 0.2,
        "align": "center",
        "angle": 0.0,
    }


def _drake_bottom_text() -> dict[str, float | str]:
    return {
        "anchor_x": 0.0,
        "anchor_y": 0.8,
        "scale_x": 1.0,
        "scale_y": 0.2,
        "align": "center",
        "angle": 0.0,
    }


def test_full_width_top_text_projects_to_top() -> None:
    slot = project_slot_position(_drake_top_text())
    assert slot.position == "top"
    assert dict(slot.box) == _drake_top_text()
    assert slot.position_override is None


def test_full_width_bottom_text_projects_to_bottom() -> None:
    slot = project_slot_position(_drake_bottom_text())
    assert slot.position == "bottom"
    assert dict(slot.box) == _drake_bottom_text()
    assert slot.position_override is None


def test_full_width_middle_text_projects_to_center() -> None:
    slot = project_slot_position(
        {"anchor_x": 0.0, "anchor_y": 0.4, "scale_x": 1.0, "scale_y": 0.2, "align": "center"}
    )
    assert slot.position == "center"
    assert slot.box["anchor_y"] == 0.4
    assert slot.position_override is None


def test_narrow_positional_text_emits_position_override() -> None:
    slot = project_slot_position(
        {"anchor_x": 0.12, "anchor_y": 0.7, "scale_x": 0.325, "scale_y": 0.1, "align": "center"}
    )
    assert slot.position == "bottom-left"
    assert slot.box["scale_x"] == 0.325
    assert slot.position_override is not None
    assert slot.position_override["anchor_x"] == 0.12


def test_narrow_right_side_projects_to_middle_right() -> None:
    slot = project_slot_position(
        {"anchor_x": 0.74, "anchor_y": 0.45, "scale_x": 0.2, "scale_y": 0.1, "align": "center"}
    )
    assert slot.position == "middle-right"
    assert slot.position_override is not None


def test_non_zero_angle_emits_position_override() -> None:
    slot = project_slot_position(
        {
            "anchor_x": 0.0,
            "anchor_y": 0.0,
            "scale_x": 1.0,
            "scale_y": 0.2,
            "align": "center",
            "angle": 15.0,
        }
    )
    assert slot.position_override is not None
    assert slot.position_override["angle"] == 15.0


def test_all_projected_positions_are_canonical() -> None:
    samples = [
        {"anchor_x": 0.0, "anchor_y": 0.0, "scale_x": 1.0, "scale_y": 0.2},
        {"anchor_x": 0.0, "anchor_y": 0.8, "scale_x": 1.0, "scale_y": 0.2},
        {"anchor_x": 0.0, "anchor_y": 0.4, "scale_x": 1.0, "scale_y": 0.2},
        {"anchor_x": 0.05, "anchor_y": 0.05, "scale_x": 0.3, "scale_y": 0.1},
        {"anchor_x": 0.7, "anchor_y": 0.05, "scale_x": 0.25, "scale_y": 0.1},
        {"anchor_x": 0.05, "anchor_y": 0.8, "scale_x": 0.3, "scale_y": 0.1},
        {"anchor_x": 0.7, "anchor_y": 0.8, "scale_x": 0.25, "scale_y": 0.1},
        {"anchor_x": 0.05, "anchor_y": 0.45, "scale_x": 0.3, "scale_y": 0.1},
        {"anchor_x": 0.7, "anchor_y": 0.45, "scale_x": 0.25, "scale_y": 0.1},
    ]
    for sample in samples:
        slot = project_slot_position(sample)
        assert slot.position in CANONICAL_POSITIONS


def test_load_upstream_template_returns_none_when_no_image(tmp_path: Path) -> None:
    template_dir = tmp_path / "noimg"
    template_dir.mkdir()
    (template_dir / "config.yml").write_text("name: NoImg\ntext: []\n")
    assert load_upstream_template(template_dir) is None


def test_load_upstream_template_skips_when_no_config(tmp_path: Path) -> None:
    template_dir = tmp_path / "nocfg"
    template_dir.mkdir()
    Image.new("RGB", (4, 4), "white").save(template_dir / "default.png")
    assert load_upstream_template(template_dir) is None


def test_slot_definitions_includes_box_for_standard_layout() -> None:
    upstream = load_upstream_template_from_yaml(
        slug="test-drake",
        yaml_body="""
name: Test Drake
source: example
keywords: []
text:
  - anchor_x: 0.0
    anchor_y: 0.0
    scale_x: 1.0
    scale_y: 0.2
    align: center
""",
    )
    defs = slot_definitions(upstream)
    assert defs == [
        {
            "name": "slot_1",
            "position": "top",
            "box": {
                "anchor_x": 0.0,
                "anchor_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 0.2,
                "align": "center",
                "angle": 0.0,
            },
        }
    ]


def load_upstream_template_from_yaml(slug: str, yaml_body: str, tmp_path: Path | None = None):
    base = Path(tempfile.mkdtemp()) if tmp_path is None else tmp_path
    template_dir = base / slug
    template_dir.mkdir()
    (template_dir / "config.yml").write_text(yaml_body)
    Image.new("RGB", (4, 4), "white").save(template_dir / "default.png")
    template = load_upstream_template(template_dir)
    assert template is not None
    return template


def test_import_upstream_corpus_writes_templates_and_manifest(tmp_path: Path) -> None:
    upstream_root = tmp_path / "upstream"
    templates_dir = upstream_root / "templates"
    templates_dir.mkdir(parents=True)
    for slug in ("alpha", "_skipme"):
        td = templates_dir / slug
        td.mkdir()
        (td / "config.yml").write_text(
            f"name: {slug.title()}\nkeywords: []\n"
            "text:\n  - anchor_x: 0.0\n    anchor_y: 0.0\n    scale_x: 1.0\n    scale_y: 0.2\n"
        )
        Image.new("RGB", (8, 8), "red").save(td / "default.png")

    db_path = tmp_path / "db.sqlite"
    repo = SQLiteTemplateRepository(db_path)
    store = FilesystemImageStore(tmp_path / "images")
    count, manifest = import_upstream_corpus(upstream_root, repo, store, "deadbeef")

    assert count == 1
    assert "alpha" in manifest
    assert "_skipme" not in manifest
    assert manifest["_upstream_commit"] == "deadbeef"
    assert repo.get("memegen-alpha").name == "Alpha"


def _import_one(
    tmp_path: Path,
    slug: str,
    source: str,
    keywords: tuple[str, ...] = (),
    enrichment: dict[str, Any] | None = None,
):
    """Import a single upstream template and return its persisted TemplateRow."""
    templates_dir = tmp_path / "upstream" / "templates" / slug
    templates_dir.mkdir(parents=True)
    if keywords:
        kw_block = "keywords:\n" + "".join(f"  - {kw}\n" for kw in keywords)
    else:
        kw_block = "keywords: []\n"
    (templates_dir / "config.yml").write_text(
        f"name: {slug.title()}\nsource: {source}\n{kw_block}"
        "text:\n  - anchor_x: 0.0\n    anchor_y: 0.0\n    scale_x: 1.0\n    scale_y: 0.2\n"
    )
    Image.new("RGB", (8, 8), "red").save(templates_dir / "default.png")

    enrichment_path: Path | None = None
    if enrichment is not None:
        enrichment_path = tmp_path / "enrichment.json"
        enrichment_path.write_text(json.dumps(enrichment))

    repo = SQLiteTemplateRepository(tmp_path / "db.sqlite")
    store = FilesystemImageStore(tmp_path / "images")
    import_upstream_corpus(
        tmp_path / "upstream", repo, store, "sha", enrichment_path=enrichment_path
    )
    return repo.get(f"memegen-{slug}")


def test_import_relocates_source_into_origin_and_clears_usage_context(tmp_path: Path) -> None:
    row = _import_one(tmp_path, "tenguy", "http://knowyourmeme.com/memes/10-guy")
    # The URL no longer pollutes the searchable usage_context field...
    assert row.metadata["usage_context"] == ""
    # ...it lives in a provenance-only origin block (no name/status, so it never
    # earns the find alias bonus).
    assert row.metadata["origin"] == {
        "source_url": "https://knowyourmeme.com/memes/10-guy"
    }
    assert "name" not in row.metadata["origin"]
    assert "status" not in row.metadata["origin"]


def test_import_upgrades_http_source_to_https(tmp_path: Path) -> None:
    # KTD7: http sources are normalized so the https-only gate keeps the link.
    row = _import_one(tmp_path, "buzz", "http://imgflip.com/memetemplate/x")
    assert row.metadata["origin"]["source_url"] == "https://imgflip.com/memetemplate/x"


def test_import_preserves_https_source(tmp_path: Path) -> None:
    row = _import_one(tmp_path, "buzz", "https://imgflip.com/memetemplate/x")
    assert row.metadata["origin"]["source_url"] == "https://imgflip.com/memetemplate/x"


def test_import_drops_origin_for_non_http_source(tmp_path: Path) -> None:
    # A scheme that cannot be normalized to https is dropped, not stored.
    row = _import_one(tmp_path, "weird", "ftp://example.com/x")
    assert "origin" not in row.metadata
    assert row.metadata["usage_context"] == ""


def test_import_without_enrichment_leaves_prose_empty(tmp_path: Path) -> None:
    row = _import_one(tmp_path, "tenguy", "https://kym.test/x", keywords=("ten", "guy"))
    assert row.metadata["description"] == ""
    assert row.metadata["emotion"] == ""
    assert row.metadata["usage_context"] == ""
    assert row.metadata["tags"] == ["ten", "guy"]


def test_import_merges_enrichment_overlay(tmp_path: Path) -> None:
    enrichment = {
        "_meta": {"memegen_commit": "sha", "model": "sonnet"},
        "tenguy": {
            "description": "Used when stating something obviously true.",
            "emotion": "smug",
            "usage_context": "reacting to an obvious fact",
            "extra_tags": ["obvious", "ten"],
        },
    }
    row = _import_one(
        tmp_path, "tenguy", "https://kym.test/x", keywords=("ten", "guy"), enrichment=enrichment
    )
    assert row.metadata["description"] == "Used when stating something obviously true."
    assert row.metadata["emotion"] == "smug"
    assert row.metadata["usage_context"] == "reacting to an obvious fact"
    # tags = keywords union extra_tags, de-duplicated and order-preserving.
    assert row.metadata["tags"] == ["ten", "guy", "obvious"]


def test_import_strips_markup_and_zero_width_from_enriched_prose(tmp_path: Path) -> None:
    # Authored prose flows to the find/MCP sink, so it is markup/zero-width
    # stripped by hard_sanitize_metadata before storage (KTD8).
    enrichment = {
        "tenguy": {
            "description": "<script>alert(1)</script>safe text",
            "emotion": "calm​x",
            "usage_context": "ok",
            "extra_tags": [],
        },
    }
    row = _import_one(tmp_path, "tenguy", "https://kym.test/x", enrichment=enrichment)
    assert "<script>" not in row.metadata["description"]
    assert "</script>" not in row.metadata["description"]
    assert "safe text" in row.metadata["description"]
    # The zero-width space is stripped, not preserved verbatim.
    assert row.metadata["emotion"] == "calmx"


def test_import_dedupes_tags_after_sanitization(tmp_path: Path) -> None:
    # Markup-laden extra_tags collapse onto existing tags after cleaning and must
    # not produce post-sanitization duplicates; empty tags are dropped.
    enrichment = {"tenguy": {"extra_tags": ["<b>ten</b>", "obvious", ""]}}
    row = _import_one(
        tmp_path, "tenguy", "https://kym.test/x", keywords=("ten", "guy"), enrichment=enrichment
    )
    assert row.metadata["tags"] == ["ten", "guy", "obvious"]


def test_import_degrades_when_enrichment_file_is_malformed(tmp_path: Path) -> None:
    # A corrupt enrichment file must not abort the seed -- it degrades to
    # relocation-only (KTD4), same as an absent file.
    templates_dir = tmp_path / "upstream" / "templates" / "tenguy"
    templates_dir.mkdir(parents=True)
    (templates_dir / "config.yml").write_text(
        "name: Tenguy\nsource: https://kym.test/x\nkeywords: []\n"
        "text:\n  - anchor_x: 0.0\n    anchor_y: 0.0\n    scale_x: 1.0\n    scale_y: 0.2\n"
    )
    Image.new("RGB", (8, 8), "red").save(templates_dir / "default.png")
    bad = tmp_path / "enrichment.json"
    bad.write_text('{"tenguy": {"description": "truncated')  # invalid JSON

    repo = SQLiteTemplateRepository(tmp_path / "db.sqlite")
    store = FilesystemImageStore(tmp_path / "images")
    count, _ = import_upstream_corpus(
        tmp_path / "upstream", repo, store, "sha", enrichment_path=bad
    )

    assert count == 1
    row = repo.get("memegen-tenguy")
    assert row.metadata["description"] == ""
    assert row.metadata["origin"] == {"source_url": "https://kym.test/x"}
