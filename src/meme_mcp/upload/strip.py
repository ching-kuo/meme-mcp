from __future__ import annotations

from io import BytesIO
from typing import Any

from PIL import Image

from meme_mcp.errors import ErrorCode, MemeMCPError
from meme_mcp.upload.validation import reject_decompression_bombs


def strip_and_reencode(content: bytes, mime: str) -> bytes:
    format_name = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime]
    try:
        with reject_decompression_bombs(), Image.open(BytesIO(content)) as image:
            output = BytesIO()
            save_kwargs: dict[str, Any] = {"format": format_name}
            save_image: Any = image
            if format_name == "JPEG":
                save_image = image.convert("RGB")
                save_kwargs["quality"] = 92
            save_image.save(output, **save_kwargs)
            return output.getvalue()
    except Image.DecompressionBombWarning as exc:
        raise MemeMCPError(
            ErrorCode.UPLOAD_REJECTED, [{"field": "file", "reason": "decompression_bomb"}]
        ) from exc
