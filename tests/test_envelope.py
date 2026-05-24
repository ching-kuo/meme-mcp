from meme_mcp.envelope import make_error, make_success
from meme_mcp.errors import ErrorCode, MemeMCPError, status_for_error


def test_success_envelope_keeps_stable_shape() -> None:
    assert make_success({"x": 1}) == {
        "ok": True,
        "data": {"x": 1},
        "error_code": None,
        "errors": None,
    }


def test_error_envelope_keeps_none_fields() -> None:
    assert make_error(ErrorCode.SLOT_MISMATCH, [{"field": "slot_fills", "reason": "extra"}]) == {
        "ok": False,
        "data": None,
        "error_code": "SLOT_MISMATCH",
        "errors": [{"field": "slot_fills", "reason": "extra"}],
    }


def test_error_codes_include_plan_surface() -> None:
    expected = {
        "INVALID_INPUT",
        "SLOT_MISMATCH",
        "UNAUTHORIZED",
        "FORBIDDEN_NOT_ALLOWLISTED",
        "RATE_LIMITED",
        "UPLOAD_REJECTED",
        "RENDER_FAILED",
        "VLM_UNAVAILABLE",
        "VLM_OUTPUT_SUSPECT",
        "DUPLICATE_TEMPLATE",
        "NOT_FOUND",
        "INTERNAL_ERROR",
    }
    assert expected <= {code.value for code in ErrorCode}


def test_exception_carries_envelope_details() -> None:
    exc = MemeMCPError(ErrorCode.UNAUTHORIZED, [{"field": "auth", "reason": "missing"}])
    assert exc.error_code is ErrorCode.UNAUTHORIZED
    assert status_for_error(exc.error_code) == 401

