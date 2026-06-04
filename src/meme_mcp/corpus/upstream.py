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
from meme_mcp.metadata_locales import merge_locales, provenance
from meme_mcp.rendering.image_store import FilesystemImageStore
from meme_mcp.upload.validation import compute_hashes
from meme_mcp.vlm.sanitize import hard_sanitize_metadata, sanitize_url

# Enrichment-file field name -> stored metadata field name. The English asset and
# the zh-TW overlay both key prose by these source names; the overlay uses "tags"
# directly while the English asset uses "extra_tags" (merged with config keywords).
_OVERLAY_PROSE_FIELDS = ("description", "emotion", "usage_context")

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
    box: Mapping[str, Any]
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
    The raw box geometry is always preserved so the renderer can reproduce the
    upstream layout. Narrow boxes or non-axis-aligned text also retain the
    legacy position_override field for external callers that already inspect it.
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

    box = MappingProxyType(
        {
            "anchor_x": anchor_x,
            "anchor_y": anchor_y,
            "scale_x": scale_x,
            "scale_y": scale_y,
            "align": align,
            "angle": angle,
        }
    )
    is_standard = full_width and angle == 0.0 and align == "center"
    return UpstreamSlot(
        position=position,
        box=box,
        position_override=None if is_standard else box,
    )


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
        entry: dict[str, Any] = {
            "name": f"slot_{i + 1}",
            "position": slot.position,
            "box": dict(slot.box),
        }
        if slot.position_override is not None:
            entry["position_override"] = dict(slot.position_override)
        definitions.append(entry)
    return definitions


def _upgrade_to_https(url: str) -> str:
    """Upgrade an http source link to https before the https-only URL gate.

    Upstream `source` values are a mix of http/https (e.g. knowyourmeme is http,
    imgflip is https). The reference hosts all serve https, so normalizing the
    scheme lets the existing https-only `sanitize_url` (store) and
    `origin_source_url_safe` (render) accept every source instead of silently
    dropping the http ones.
    """
    stripped = url.strip()
    if stripped.lower().startswith("http://"):
        return "https://" + stripped[len("http://") :]
    return stripped


def _load_enrichment(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load slug -> enriched fields from the committed enrichment file.

    Absent file or absent slug entry degrades to relocation-only (empty prose).
    The top-level `_meta` provenance header and any non-dict entry are skipped.
    """
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # A corrupt/half-written/non-UTF-8 enrichment file degrades to
        # relocation-only rather than aborting the whole 209-row seed (KTD4).
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if not key.startswith("_") and isinstance(value, dict)
    }


def _attach_zh_tw_locale(
    metadata: dict[str, Any], overlay: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach an unsanitized locales['zh-TW'] block with machine/drift-pass meta.

    The block carries only the four overlay-scoped fields (name stays English by
    design; localize() falls back to the top-level English name). Per-field `_meta`
    records machine provenance with a passing drift status; the overlay is the
    drift-gated GENERATE output, so values reaching import already passed the gate.
    The caller runs `hard_sanitize_metadata`, whose locales dispatch sanitizes this
    block (prose capped, tags per-item capped, `_meta` enum-validated).
    """
    block: dict[str, Any] = {}
    for field in _OVERLAY_PROSE_FIELDS:
        value = overlay.get(field)
        if isinstance(value, str) and value.strip():
            block[field] = value
    tags = [tag for tag in (overlay.get("tags") or []) if isinstance(tag, str) and tag.strip()]
    if tags:
        block["tags"] = tags
    if not block:
        return metadata
    block["_meta"] = {field: provenance("machine", drift="pass") for field in block}
    result = dict(metadata)
    result["locales"] = {"zh-TW": block}
    return result


def _build_metadata(
    upstream: UpstreamTemplate,
    enriched: Mapping[str, Any],
    zh_tw_overlay: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble + sanitize one template's metadata: relocate URL, merge enrichment.

    The `source` URL moves into a provenance-only `origin` block (no name/status,
    so it never earns the find alias bonus) and out of `usage_context`, which
    stops it polluting the keyword index and embedding. Enriched prose overlays
    the empty defaults. The whole dict passes through `hard_sanitize_metadata`
    so authored prose can never reach the find/MCP sink unsanitized.
    """
    tags = list(upstream.keywords)
    tags += [tag for tag in (enriched.get("extra_tags") or []) if isinstance(tag, str)]
    metadata: dict[str, Any] = {
        "name": upstream.name,
        "description": str(enriched.get("description", "")),
        "emotion": str(enriched.get("emotion", "")),
        "usage_context": str(enriched.get("usage_context", "")),
        "tags": tags,
        "format": "static",
    }
    source_url = sanitize_url(_upgrade_to_https(upstream.source_url))
    if source_url:
        metadata["origin"] = {"source_url": source_url}
    if zh_tw_overlay:
        metadata = _attach_zh_tw_locale(metadata, zh_tw_overlay)
    metadata = hard_sanitize_metadata(metadata)
    # De-duplicate and drop empties on the SANITIZED tag value: markup-laden input
    # can collapse onto an existing tag after cleaning (e.g. "<b>ten</b>" -> "ten"),
    # which the pre-sanitization union would not have caught.
    deduped: list[str] = []
    for tag in metadata["tags"]:
        if tag and tag not in deduped:
            deduped.append(tag)
    metadata["tags"] = deduped
    return metadata


def import_upstream_corpus(
    upstream_root: Path,
    repository: SQLiteTemplateRepository,
    image_store: FilesystemImageStore,
    upstream_commit_sha: str,
    enrichment_path: Path | None = None,
    zh_tw_enrichment_path: Path | None = None,
) -> tuple[int, dict[str, str]]:
    """Walk upstream/templates/*, persist templates, return (count, manifest).

    The manifest maps slug -> SHA-256 of the imported image bytes. Pinning the
    upstream commit + per-template hashes is what makes seed-memegen reproducible
    across machines. `enrichment_path` points at the committed web-grounded
    English enrichment file (optional; absence degrades to relocation-only).
    `zh_tw_enrichment_path` points at the committed zh-TW overlay (optional;
    absence or a missing slug degrades that row to English-only).
    """
    templates_dir = upstream_root / "templates"
    if not templates_dir.is_dir():
        raise FileNotFoundError(f"no templates/ under {upstream_root}")
    enrichment = _load_enrichment(enrichment_path)
    # The zh-TW overlay loads with the same loader/degradation as the English
    # enrichment: an absent, corrupt, or non-UTF-8 overlay (or a missing slug)
    # degrades that row to English-only rather than aborting the seed.
    zh_tw_overlay = _load_enrichment(zh_tw_enrichment_path)
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
        template_id = f"memegen-{upstream.slug}"
        built = _build_metadata(
            upstream,
            enrichment.get(upstream.slug, {}),
            zh_tw_overlay.get(upstream.slug),
        )
        # Read-merge-write so a re-seed never clobbers a human-authored zh-TW
        # field with the freshly rebuilt machine overlay. merge_locales keeps the
        # rebuilt English top level authoritative while honoring human-wins on the
        # locale block; the importer is single-threaded so the read-write window
        # is safe without locking.
        try:
            stored_metadata: dict[str, Any] | None = repository.get(template_id).metadata
        except KeyError:
            stored_metadata = None
        metadata = merge_locales(stored_metadata, built)
        repository.upsert(
            TemplateCreate(
                template_id=template_id,
                slug=upstream.slug,
                name=upstream.name,
                source="memegen",
                metadata=metadata,
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
