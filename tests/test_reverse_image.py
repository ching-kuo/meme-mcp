from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from meme_mcp.reverse_image.client import (
    MAX_INLINE_BYTES,
    GoogleVisionClient,
    WebDetectionResult,
)


def _label(text: str) -> SimpleNamespace:
    return SimpleNamespace(label=text, language_code="en")


def _entity(description: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(entity_id="e", score=score, description=description)


def _page(title: str, url: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(
        page_title=title,
        url=url,
        score=score,
        full_matching_images=[],
        partial_matching_images=[],
    )


def _web_detection(
    *,
    labels: list[SimpleNamespace] | None = None,
    entities: list[SimpleNamespace] | None = None,
    pages: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        best_guess_labels=labels or [],
        web_entities=entities or [],
        pages_with_matching_images=pages or [],
    )


def _response(web: SimpleNamespace, *, error_message: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        error=SimpleNamespace(message=error_message, code=0),
        web_detection=web,
    )


class FakeAnnotator:
    """Duck-typed stand-in for ImageAnnotatorClient (no Protocol, no network)."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls = 0
        self.last_timeout: float | None = None

    def web_detection(self, *, image: Any, timeout: float) -> Any:
        del image
        self.calls += 1
        self.last_timeout = timeout
        return self._response


class RaisingAnnotator:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def web_detection(self, *, image: Any, timeout: float) -> Any:
        del image, timeout
        self.calls += 1
        raise self._exc


def test_strong_match_above_floor_is_success() -> None:
    web = _web_detection(
        labels=[_label("Is This a Pigeon?")],
        entities=[_entity("Is This a Pigeon?", 0.95), _entity("anime", 0.4)],
        pages=[_page("Know Your Meme", "https://knowyourmeme.com/pigeon", 0.9)],
    )
    client = GoogleVisionClient(FakeAnnotator(_response(web)), confidence_floor=0.5)

    result = client.detect(b"png-bytes")

    assert result.status == "success"
    assert result.grounding is not None
    assert result.grounding.best_guess == "Is This a Pigeon?"
    assert "anime" in result.grounding.entities
    assert result.origin is not None
    assert result.origin.name == "Is This a Pigeon?"
    assert result.origin.source_url == "https://knowyourmeme.com/pigeon"


def test_weak_match_below_floor_is_low_confidence() -> None:
    web = _web_detection(
        labels=[_label("blurry photo")],
        entities=[_entity("blurry photo", 0.1)],
        pages=[_page("Some Blog", "https://example.com/post", 0.2)],
    )
    client = GoogleVisionClient(FakeAnnotator(_response(web)), confidence_floor=0.5)

    result = client.detect(b"png-bytes")

    # Origin candidate is captured for review, grounding present but the status
    # tells the service to suppress R3 precedence.
    assert result.status == "low_confidence"
    assert result.grounding is not None
    assert result.origin is not None
    assert result.origin.name == "blurry photo"


def test_empty_web_detection_is_no_match() -> None:
    client = GoogleVisionClient(FakeAnnotator(_response(_web_detection())))

    result = client.detect(b"png-bytes")

    assert result == WebDetectionResult("no_match", None, None)


def test_deadline_exceeded_maps_to_timeout_and_logs(caplog) -> None:
    annotator = RaisingAnnotator(gexc.DeadlineExceeded("slow"))
    client = GoogleVisionClient(annotator)

    with caplog.at_level("WARNING"):
        result = client.detect(b"png-bytes")

    assert result.status == "timeout"
    assert annotator.calls == 1
    assert any("timeout" in r.message for r in caplog.records)


def test_retry_error_maps_to_timeout() -> None:
    client = GoogleVisionClient(RaisingAnnotator(gexc.RetryError("retry", cause=None)))
    assert client.detect(b"png-bytes").status == "timeout"


def test_api_call_error_maps_to_error_and_logs(caplog) -> None:
    annotator = RaisingAnnotator(gexc.InvalidArgument("bad request detail"))
    client = GoogleVisionClient(annotator)

    with caplog.at_level("WARNING"):
        result = client.detect(b"png-bytes")

    assert result.status == "error"
    # The provider's message must never leak into logs (KTD10).
    assert not any("bad request detail" in r.message for r in caplog.records)
    assert any("api_error" in r.message for r in caplog.records)


def test_in_body_error_message_maps_to_error_without_leaking(caplog) -> None:
    web = _web_detection(labels=[_label("x")])
    response = _response(web, error_message="quota exceeded for project 12345")
    client = GoogleVisionClient(FakeAnnotator(response))

    with caplog.at_level("WARNING"):
        result = client.detect(b"png-bytes")

    assert result.status == "error"
    assert not any("12345" in r.message for r in caplog.records)


def test_oversized_bytes_skip_the_sdk() -> None:
    annotator = FakeAnnotator(_response(_web_detection(labels=[_label("x")])))
    client = GoogleVisionClient(annotator)

    result = client.detect(b"x" * (MAX_INLINE_BYTES + 1))

    assert result.status == "no_match"
    assert annotator.calls == 0


def test_origin_name_falls_back_to_top_entity_when_no_label() -> None:
    web = _web_detection(
        entities=[_entity("Distracted Boyfriend", 0.8)],
        pages=[_page("KYM", "https://knowyourmeme.com/db", 0.7)],
    )
    client = GoogleVisionClient(FakeAnnotator(_response(web)), confidence_floor=0.5)

    result = client.detect(b"png-bytes")

    assert result.status == "success"
    assert result.origin is not None
    assert result.origin.name == "Distracted Boyfriend"


def test_timeout_seconds_passed_to_sdk() -> None:
    annotator = FakeAnnotator(_response(_web_detection(labels=[_label("x")])))
    client = GoogleVisionClient(annotator, timeout_seconds=8.0)

    client.detect(b"png-bytes")

    assert annotator.last_timeout == 8.0


@pytest.mark.parametrize("blank", [b"", b"png"])
def test_never_raises_on_any_input(blank: bytes) -> None:
    client = GoogleVisionClient(FakeAnnotator(_response(_web_detection())))
    assert isinstance(client.detect(blank), WebDetectionResult)
