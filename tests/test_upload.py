from io import BytesIO

import pytest
from PIL import Image

from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.upload.dedupe import DuplicateIndex, check_duplicates
from meme_mcp.upload.strip import strip_and_reencode
from meme_mcp.upload.validation import compute_hashes, validate_upload


def png_bytes(color: str = "white") -> bytes:
    image = Image.new("RGB", (64, 64), color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_valid_png_sanitizes_without_exif() -> None:
    content = png_bytes()
    result = validate_upload(content, "image/png", "x.png")
    assert result.mime == "image/png"
    sanitized = strip_and_reencode(content, result.mime)
    assert Image.open(BytesIO(sanitized)).info.get("exif") is None


def test_size_gate_rejects_before_decode() -> None:
    with pytest.raises(MemeMCPError) as caught:
        validate_upload(b"x" * (10 * 1024 * 1024 + 1), "image/png", "x.png")
    assert caught.value.error_code is ErrorCode.UPLOAD_REJECTED
    assert caught.value.errors[0]["reason"] == "size"


def test_mime_mismatch_rejects() -> None:
    with pytest.raises(MemeMCPError) as caught:
        validate_upload(png_bytes(), "image/jpeg", "x.jpg")
    assert caught.value.errors[0]["reason"] == "mime_mismatch"


def test_duplicate_index_blocks_exact_and_warns_near_duplicate() -> None:
    index = DuplicateIndex()
    index.add("template-1", "abc", "0000000000000000")
    assert check_duplicates(index, "abc", "ffffffffffffffff").action == "block"
    assert check_duplicates(index, "def", "0000000000000001").action == "warn"
    assert check_duplicates(index, "ghi", "ffffffffffffffff").action == "accept"


def test_compute_hashes_returns_sha256_and_dhash_for_sanitized_bytes() -> None:
    content = strip_and_reencode(png_bytes(), "image/png")
    exact, perceptual = compute_hashes(content)
    assert len(exact) == 64
    assert len(perceptual) == 16
