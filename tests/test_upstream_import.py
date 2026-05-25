from __future__ import annotations

import tempfile
from pathlib import Path

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
    assert slot.position_override is None


def test_full_width_bottom_text_projects_to_bottom() -> None:
    slot = project_slot_position(_drake_bottom_text())
    assert slot.position == "bottom"
    assert slot.position_override is None


def test_full_width_middle_text_projects_to_center() -> None:
    slot = project_slot_position(
        {"anchor_x": 0.0, "anchor_y": 0.4, "scale_x": 1.0, "scale_y": 0.2, "align": "center"}
    )
    assert slot.position == "center"
    assert slot.position_override is None


def test_narrow_positional_text_emits_position_override() -> None:
    slot = project_slot_position(
        {"anchor_x": 0.12, "anchor_y": 0.7, "scale_x": 0.325, "scale_y": 0.1, "align": "center"}
    )
    assert slot.position == "bottom-left"
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


def test_slot_definitions_omits_override_for_standard_layout() -> None:
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
    assert defs == [{"name": "slot_1", "position": "top"}]


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
