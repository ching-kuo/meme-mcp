"""Shared analyze/approve upload pipeline.

Both front doors call this module: the PAT-authenticated ``/api/uploads/*``
routes and the session-authenticated ``/upload/*`` web endpoints. Centralising
the pipeline here is the anti-drift guarantee (KTD2): the rate-limiter ``hit``,
the base64 decode (and its error envelope), validation, EXIF-strip/re-encode,
hashing, dedupe, VLM enrichment, storage, and the name-required check all live
here so neither front door can diverge in behavior.

The pre-buffer body-size guard is deliberately NOT in this service. By the time
a handler holds a parsed ``payload``, Starlette has already buffered the body,
so a true pre-buffer rejection must be app-level ASGI middleware (KTD11).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from dataclasses import dataclass
from typing import Any, Protocol

from meme_mcp.auth.depends import Friend, require_write
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.db.uploads import PendingUpload, PendingUploadStore
from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.rendering.image_store import ImageStore
from meme_mcp.upload.dedupe import DuplicateIndex, check_duplicates
from meme_mcp.upload.strip import strip_and_reencode
from meme_mcp.upload.validation import compute_hashes, validate_upload
from meme_mcp.vlm.sanitize import flag_anomalies, hard_sanitize_metadata

PLACEHOLDER_NAME = "Uploaded Meme"


class RateLimiter(Protocol):
    def hit(self, key: str) -> None: ...


class VLMEnricher(Protocol):
    def enrich_template(
        self,
        image_bytes: bytes,
        title_hint: str | None = None,
        grounding: str | None = None,
        *,
        grounding_authoritative: bool = True,
    ) -> Any: ...


class TemplateRepository(Protocol):
    def upsert(self, template: TemplateCreate) -> None: ...


@dataclass(frozen=True)
class UploadServiceDeps:
    """Collaborators the shared pipeline needs, supplied by the caller."""

    upload_limiter: RateLimiter
    vlm_client: VLMEnricher
    image_store: ImageStore
    pending_uploads: PendingUploadStore
    templates: TemplateRepository
    duplicate_index: DuplicateIndex


@dataclass(frozen=True)
class AnalyzeResult:
    pending_upload_id: str
    metadata: dict[str, Any]
    slot_definitions: list[dict[str, Any]]
    duplicate_action: str
    duplicate_template_id: str | None
    suspect_flags: list[str]


@dataclass(frozen=True)
class ApproveResult:
    template_id: str
    slug: str


async def analyze_image(
    *,
    content_base64: str,
    declared_mime: str,
    filename: str,
    title_hint: object,
    friend_login: str,
    deps: UploadServiceDeps,
) -> AnalyzeResult:
    """Validate, strip, dedupe and enrich an uploaded image; persist a pending row.

    Owns the rate-limiter ``hit`` and the base64 decode so ordering and the
    ``INVALID_INPUT`` error envelope are identical across both front doors
    (KTD2). Raises ``MemeMCPError`` on rejection (rate limit, bad base64,
    validation failure, exact-hash duplicate).
    """
    deps.upload_limiter.hit(friend_login)
    hint = _normalize_title_hint(title_hint)
    content = _decode_base64(content_base64)
    validated = validate_upload(content, declared_mime, filename)
    sanitized = strip_and_reencode(content, validated.mime)
    exact_hash, perceptual_hash = compute_hashes(sanitized)
    duplicate = check_duplicates(deps.duplicate_index, exact_hash, perceptual_hash)
    if duplicate.action == "block":
        raise MemeMCPError(
            ErrorCode.DUPLICATE_TEMPLATE,
            [{"field": "file", "reason": f"duplicate:{duplicate.template_id}"}],
        )
    enrichment = await asyncio.to_thread(
        deps.vlm_client.enrich_template,
        sanitized,
        hint,
    )
    if enrichment.status == "success" and enrichment.metadata is not None:
        metadata = enrichment.metadata
        suspect_flags = enrichment.suspect_flags
    else:
        metadata = _blank_upload_metadata(hint)
        suspect_flags = [f"vlm_{enrichment.status}"]
    slot_definitions = slot_definitions_for(metadata)
    # Record the pending row BEFORE writing the blob (KTD8). create-then-put makes the
    # row -- and thus the image_path reference -- observable to the gc sweep's live-
    # reference check before the content-addressed blob is (re)written, closing the
    # window where a sweep could reclaim a stale orphan's blob that a re-upload of
    # identical bytes is about to reuse. validate_upload already proved
    # detect_mime(content) == declared and returned it as validated.mime, so that is
    # the content-of-record MIME (R7); image_store.put is idempotent on the path.
    ext = _extension_for_mime(validated.mime)
    image_path = deps.image_store.path_for(sanitized, ext)
    pending = deps.pending_uploads.create(
        friend_login=friend_login,
        image_path=image_path,
        metadata=metadata,
        slot_definitions=slot_definitions,
        exact_hash=exact_hash,
        perceptual_hash=perceptual_hash,
        duplicate_action=duplicate.action,
        duplicate_template_id=duplicate.template_id,
        suspect_flags=suspect_flags,
    )
    deps.image_store.put(sanitized, ext)
    return AnalyzeResult(
        pending_upload_id=pending.upload_id,
        metadata=pending.metadata,
        slot_definitions=pending.slot_definitions,
        duplicate_action=pending.duplicate_action,
        duplicate_template_id=pending.duplicate_template_id,
        suspect_flags=pending.suspect_flags,
    )


def approve_pending(
    *,
    pending: PendingUpload,
    actor: Friend,
    metadata_overrides: dict[str, Any] | None,
    slot_overrides: list[Any] | None,
    ack_suspect: bool,
    deps: UploadServiceDeps,
) -> ApproveResult:
    """Promote a pending upload to a template after validating the metadata.

    The caller is responsible for the owner-scoped ``pending_uploads.get`` so
    that ``NOT_FOUND`` stays opaque per front door. This function enforces write
    capability and the name-required check, upserts the template
    (``source="friend"``), and deletes the pending row.
    """
    require_write(actor)
    metadata_in = metadata_overrides if metadata_overrides is not None else pending.metadata
    metadata = _validated_metadata(metadata_in, pending.suspect_flags, ack_suspect)
    if slot_overrides is not None:
        slot_definitions: list[Any] = slot_overrides
    else:
        slot_definitions = pending.slot_definitions
    name = str(metadata["name"])
    template_id = _template_id(name, pending.exact_hash)
    deps.templates.upsert(
        TemplateCreate(
            template_id=template_id,
            slug=template_id,
            name=name,
            source="friend",
            metadata=metadata,
            slot_definitions=[slot for slot in slot_definitions if isinstance(slot, dict)],
            image_path=pending.image_path,
            perceptual_hash=pending.perceptual_hash,
            exact_hash=pending.exact_hash,
        )
    )
    deps.pending_uploads.delete(pending.upload_id)
    return ApproveResult(template_id=template_id, slug=template_id)


def _decode_base64(content_base64: str) -> bytes:
    try:
        return base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MemeMCPError(
            ErrorCode.INVALID_INPUT,
            [{"field": "content_base64", "reason": "base64"}],
        ) from exc


def _normalize_title_hint(value: object) -> str | None:
    """Coerce a caller-supplied title hint to a clean str or None.

    Owned by the service (not the front doors) so the PAT and web routes feed
    analyze_image identically (KTD2): an empty or whitespace-only hint becomes None.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extension_for_mime(mime: str) -> str:
    return {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]


def slot_definitions_for(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    slots = metadata.get("slot_definitions")
    if isinstance(slots, list):
        typed_slots = [slot for slot in slots if isinstance(slot, dict)]
        if typed_slots:
            return typed_slots
    return [{"name": "top", "position": "top"}, {"name": "bottom", "position": "bottom"}]


def _blank_upload_metadata(title_hint: str | None) -> dict[str, Any]:
    return {
        "name": title_hint or PLACEHOLDER_NAME,
        "description": "",
        "emotion": "",
        "usage_context": "",
        "tags": [],
        "format": "static",
        "slot_definitions": [{"name": "top", "position": "top"}],
    }


def _validated_metadata(
    metadata: dict[str, Any],
    suspect_flags: list[str],
    ack_suspect: bool,
) -> dict[str, Any]:
    raw_flags = flag_anomalies(metadata)
    cleaned = hard_sanitize_metadata(metadata)
    flags = sorted(set(suspect_flags) | set(raw_flags) | set(flag_anomalies(cleaned)))
    if flags and not ack_suspect:
        raise MemeMCPError(
            ErrorCode.VLM_OUTPUT_SUSPECT,
            [{"field": "metadata", "reason": ",".join(flags)}],
        )
    # Name is required independently of (and after) the ack gate (KTD7/R14), so
    # an acknowledged-suspect upload with a blank or placeholder name still fails.
    name = str(cleaned.get("name", "")).strip()
    if not name or name == PLACEHOLDER_NAME:
        raise MemeMCPError(
            ErrorCode.INVALID_INPUT,
            [{"field": "name", "reason": "name_required"}],
        )
    required = ["name", "description", "emotion", "usage_context", "tags", "format"]
    missing = [key for key in required if key not in cleaned]
    if missing:
        raise MemeMCPError(
            ErrorCode.INVALID_INPUT,
            [{"field": "metadata", "reason": f"missing:{','.join(missing)}"}],
        )
    if cleaned.get("format") != "static":
        raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "format", "reason": "static"}])
    if not isinstance(cleaned.get("tags"), list):
        raise MemeMCPError(ErrorCode.INVALID_INPUT, [{"field": "tags", "reason": "list"}])
    cleaned["slot_definitions"] = slot_definitions_for(cleaned)
    return cleaned


def _template_id(name: str, exact_hash: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "uploaded-meme"
    return f"{slug}-{exact_hash[:8]}"
