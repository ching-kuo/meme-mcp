from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

import imagehash
from PIL import Image, UnidentifiedImageError

from meme_mcp.errors import ErrorCode, MemeMCPError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_MIMES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}


@dataclass(frozen=True)
class ValidationResult:
    mime: str
    size_bytes: int


def detect_mime(content: bytes) -> str | None:
    try:
        import magic

        detected = magic.from_buffer(content, mime=True)
        if detected in {"image/png", "image/jpeg", "image/webp"}:
            return str(detected)
    except (ImportError, OSError, AttributeError):
        pass
    for mime, prefix in ALLOWED_MIMES.items():
        if content.startswith(prefix):
            return mime
    if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        return "image/webp"
    return None


def validate_upload(content: bytes, declared_mime: str, declared_filename: str) -> ValidationResult:
    del declared_filename
    if len(content) > MAX_UPLOAD_BYTES:
        raise MemeMCPError(ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "size"}])
    detected = detect_mime(content)
    if detected is None:
        raise MemeMCPError(
            ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "unsupported_mime"}]
        )
    if detected != declared_mime:
        raise MemeMCPError(
            ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "mime_mismatch"}]
        )
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
    except Image.DecompressionBombWarning as exc:
        raise MemeMCPError(
            ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "decompression_bomb"}]
        ) from exc
    except (Image.DecompressionBombError, MemoryError) as exc:
        raise MemeMCPError(
            ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "decompression_bomb"}]
        ) from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise MemeMCPError(
            ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "unreadable_image"}]
        ) from exc
    return ValidationResult(mime=detected, size_bytes=len(content))


def compute_hashes(content: bytes) -> tuple[str, str]:
    exact_hash = hashlib.sha256(content).hexdigest()
    with Image.open(BytesIO(content)) as image:
        perceptual_hash = str(imagehash.dhash(image))
    return exact_hash, perceptual_hash
