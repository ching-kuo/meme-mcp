from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

import imagehash
import pytest
import yaml
from PIL import Image

from meme_mcp.corpus.upstream import project_slot_position
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.rendering.pipeline import TemplateSpec, render_meme

GOLDEN_DIR = Path(__file__).parent.parent / "assets" / "golden" / "memegen-parity"
UPSTREAM_TEMPLATES = Path("/tmp/memegen-upstream/templates")

HAMMING_THRESHOLD = 8

GOLDEN_CASES: list[dict[str, Any]] = [
    {
        "slug": "drake",
        "fills": ["YAML configs", "TOML configs"],
        "xfail": "vertically split panels need per-half anchor tuning",
    },
    {"slug": "db", "fills": ["company culture", "crypto bro", "burnout"]},
    {"slug": "spongebob", "fills": ["why yes i did", ""]},
    {"slug": "rollsafe", "fills": ["cant have bugs", "if you dont deploy"]},
    {"slug": "doge", "fills": ["such meme", "very render"]},
    {
        "slug": "fry",
        "fills": ["not sure if bug", "or feature"],
        "xfail": "two-line wrap differs from memegen layout",
    },
    {
        "slug": "success",
        "fills": ["fixed bug", "on first try"],
        "xfail": "small image; stroke proportions diverge under resize",
    },
    {"slug": "grumpycat", "fills": ["no", ""]},
    {"slug": "philosoraptor", "fills": ["if logs are smart", "why cant they find bugs"]},
    {"slug": "aag", "fills": ["aliens", "did it"]},
]


def _load_template_spec(slug: str) -> TemplateSpec:
    template_dir = UPSTREAM_TEMPLATES / slug
    image_path: Path | None = None
    for name in ("default.png", "default.jpg", "default.gif"):
        candidate = template_dir / name
        if candidate.is_file():
            image_path = candidate
            break
    if image_path is None:
        pytest.skip(f"upstream image missing for {slug}")
    config = template_dir / "config.yml"
    if not config.is_file():
        pytest.skip(f"upstream config missing for {slug}")
    cfg = yaml.safe_load(config.read_text()) or {}
    slots = [
        {"name": f"slot_{i + 1}", "position": project_slot_position(t).position}
        for i, t in enumerate(cfg.get("text") or [])
    ]
    return TemplateSpec(
        template_id=f"memegen-{slug}",
        image_bytes=image_path.read_bytes(),
        slots=slots,
    )


def _hamming_distance(rendered: bytes, reference: bytes) -> int:
    rendered_img = Image.open(BytesIO(rendered)).convert("RGB")
    reference_img = Image.open(BytesIO(reference)).convert("RGB")
    # Normalize size — memegen renders at varying resolutions.
    target = (256, 256)
    rendered_img = rendered_img.resize(target)
    reference_img = reference_img.resize(target)
    diff = imagehash.dhash(rendered_img) - imagehash.dhash(reference_img)
    return int(diff)


def _case_id(case: dict[str, Any]) -> Any:
    if "xfail" in case:
        marks = pytest.mark.xfail(reason=case["xfail"], strict=True)
        return pytest.param(case, id=case["slug"], marks=marks)
    return pytest.param(case, id=case["slug"])


@pytest.mark.parametrize("case", [_case_id(case) for case in GOLDEN_CASES])
def test_visual_parity_against_memegen_reference(case: dict[str, Any], tmp_path: Path) -> None:
    if not UPSTREAM_TEMPLATES.is_dir():
        pytest.skip("upstream memegen clone not present at /tmp/memegen-upstream")
    reference_path = GOLDEN_DIR / f"{case['slug']}.reference.png"
    if not reference_path.is_file():
        pytest.skip(f"no reference render for {case['slug']}")

    spec = _load_template_spec(case["slug"])
    store = FilesystemImageStore(tmp_path / "renders")
    rendered = render_meme(spec, case["fills"], store)
    distance = _hamming_distance(rendered.bytes, reference_path.read_bytes())
    _record_distance(case["slug"], distance)
    assert distance <= HAMMING_THRESHOLD, (
        f"{case['slug']}: dhash distance {distance} > threshold {HAMMING_THRESHOLD}"
    )


def _record_distance(slug: str, distance: int) -> None:
    path = Path(__file__).parent.parent / "assets" / "golden" / "parity-distances.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, int] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {}
    data[slug] = distance
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
