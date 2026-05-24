from __future__ import annotations

from typing import Any, TypedDict

from meme_mcp.errors import ErrorCode, ErrorDetail


class Envelope(TypedDict):
    ok: bool
    data: Any | None
    error_code: str | None
    errors: list[ErrorDetail] | None


def make_success(data: Any) -> Envelope:
    return {"ok": True, "data": data, "error_code": None, "errors": None}


def make_error(code: ErrorCode, errors: list[ErrorDetail] | None = None) -> Envelope:
    return {"ok": False, "data": None, "error_code": code.value, "errors": errors or []}

