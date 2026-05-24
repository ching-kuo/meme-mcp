from __future__ import annotations

from enum import StrEnum
from typing import TypedDict


class ErrorDetail(TypedDict):
    field: str
    reason: str


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    SLOT_MISMATCH = "SLOT_MISMATCH"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    FORBIDDEN_NOT_ALLOWLISTED = "FORBIDDEN_NOT_ALLOWLISTED"
    RATE_LIMITED = "RATE_LIMITED"
    UPLOAD_REJECTED = "UPLOAD_REJECTED"
    RENDER_FAILED = "RENDER_FAILED"
    VLM_UNAVAILABLE = "VLM_UNAVAILABLE"
    VLM_OUTPUT_SUSPECT = "VLM_OUTPUT_SUSPECT"
    DUPLICATE_TEMPLATE = "DUPLICATE_TEMPLATE"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class MemeMCPError(Exception):
    def __init__(self, error_code: ErrorCode, errors: list[ErrorDetail] | None = None) -> None:
        self.error_code = error_code
        self.errors = errors or []
        super().__init__(error_code.value)


def status_for_error(error_code: ErrorCode) -> int:
    return {
        ErrorCode.INVALID_INPUT: 400,
        ErrorCode.SLOT_MISMATCH: 400,
        ErrorCode.UNAUTHORIZED: 401,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.FORBIDDEN_NOT_ALLOWLISTED: 403,
        ErrorCode.RATE_LIMITED: 429,
        ErrorCode.UPLOAD_REJECTED: 400,
        ErrorCode.RENDER_FAILED: 500,
        ErrorCode.VLM_UNAVAILABLE: 502,
        ErrorCode.VLM_OUTPUT_SUSPECT: 400,
        ErrorCode.DUPLICATE_TEMPLATE: 409,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.INTERNAL_ERROR: 500,
    }[error_code]

