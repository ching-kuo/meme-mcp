from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from meme_mcp.db.templates import SQLiteTemplateRepository, TemplateCreate
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.upload.validation import compute_hashes

CANONICAL_POSITIONS = frozenset(
    {
        "top",
        "bottom",
        "center",
        "top-left",
        "top-right",
        "bottom-left",
        "bottom-right",
        "middle-left",
        "middle-right",
    }
)


@dataclass(frozen=True)
class UpstreamSlot:
    position: str
    position_override: Mapping[str, Any] | None


@dataclass(frozen=True)
class UpstreamTemplate:
    slug: str
    name: str
    source_url: str
    keywords: tuple[str, ...]
    slots: tuple[UpstreamSlot, ...]
    image_path: Path


def project_slot_position(text_entry: dict[str, Any]) -> UpstreamSlot:
    """Project a memegen text[] entry into a canonical 9-band position.

    The 9-band enum matches the slot_definitions.position used by the renderer.
    For narrow boxes or non-axis-aligned text, the raw anchors survive as
    position_override so the renderer can reproduce the upstream layout exactly.
    """
    anchor_x = float(text_entry.get("anchor_x", 0.0))
    anchor_y = float(text_entry.get("anchor_y", 0.0))
    scale_x = float(text_entry.get("scale_x", 1.0))
    scale_y = float(text_entry.get("scale_y", 0.2))
    align = str(text_entry.get("align", "center"))
    angle = float(text_entry.get("angle", 0.0))

    center_x = anchor_x + scale_x / 2
    center_y = anchor_y + scale_y / 2
    full_width = scale_x >= 0.9

    if center_y < 0.33:
        vertical = "top"
    elif center_y > 0.67:
        vertical = "bottom"
    else:
        vertical = "center" if full_width else "middle"

    if full_width:
        position = vertical
    else:
        horizontal = "left" if center_x < 0.5 else "right"
        position = f"{vertical}-{horizontal}"

    is_standard = full_width and angle == 0.0 and align == "center"
    position_override: Mapping[str, Any] | None = (
        None
        if is_standard
        else MappingProxyType(
            {
                "anchor_x": anchor_x,
                "anchor_y": anchor_y,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "align": align,
                "angle": angle,
            }
        )
    )
    return UpstreamSlot(position=position, position_override=position_override)


def load_upstream_template(template_dir: Path) -> UpstreamTemplate | None:
    config_path = template_dir / "config.yml"
    if not config_path.is_file():
        return None
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    image_path = _resolve_default_image(template_dir)
    if image_path is None:
        return None
    text_entries = config.get("text") or []
    slots = tuple(
        project_slot_position(entry) for entry in text_entries if isinstance(entry, dict)
    )
    keywords = tuple(
        kw for kw in (config.get("keywords") or []) if isinstance(kw, str) and kw.strip()
    )
    return UpstreamTemplate(
        slug=template_dir.name,
        name=str(config.get("name") or template_dir.name).strip(),
        source_url=str(config.get("source") or "").strip(),
        keywords=keywords,
        slots=slots,
        image_path=image_path,
    )


def _resolve_default_image(template_dir: Path) -> Path | None:
    for name in ("default.png", "default.jpg", "default.gif"):
        candidate = template_dir / name
        if candidate.is_file():
            return candidate
    return None


def slot_definitions(template: UpstreamTemplate) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for i, slot in enumerate(template.slots):
        entry: dict[str, Any] = {"name": f"slot_{i + 1}", "position": slot.position}
        if slot.position_override is not None:
            entry["position_override"] = dict(slot.position_override)
        definitions.append(entry)
    return definitions


def import_upstream_corpus(
    upstream_root: Path,
    repository: SQLiteTemplateRepository,
    image_store: FilesystemImageStore,
    upstream_commit_sha: str,
) -> tuple[int, dict[str, str]]:
    """Walk upstream/templates/*, persist templates, return (count, manifest).

    The manifest maps slug -> SHA-256 of the imported image bytes. Pinning the
    upstream commit + per-template hashes is what makes seed-memegen reproducible
    across machines.
    """
    templates_dir = upstream_root / "templates"
    if not templates_dir.is_dir():
        raise FileNotFoundError(f"no templates/ under {upstream_root}")
    manifest: dict[str, str] = {"_upstream_commit": upstream_commit_sha}
    count = 0
    for entry in sorted(templates_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        upstream = load_upstream_template(entry)
        if upstream is None or not upstream.slots:
            continue
        image_bytes = upstream.image_path.read_bytes()
        exact_hash, perceptual_hash = compute_hashes(image_bytes)
        extension = upstream.image_path.suffix.lstrip(".") or "png"
        stored_path = image_store.put(image_bytes, extension)
        repository.upsert(
            TemplateCreate(
                template_id=f"memegen-{upstream.slug}",
                slug=upstream.slug,
                name=upstream.name,
                source="memegen",
                metadata={
                    "name": upstream.name,
                    "description": "",
                    "emotion": "",
                    "usage_context": upstream.source_url,
                    "tags": list(upstream.keywords),
                    "format": "static",
                },
                slot_definitions=slot_definitions(upstream),
                image_path=stored_path,
                perceptual_hash=perceptual_hash,
                exact_hash=exact_hash,
            )
        )
        manifest[upstream.slug] = hashlib.sha256(image_bytes).hexdigest()
        count += 1
    return count, manifest


def write_manifest(manifest: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
