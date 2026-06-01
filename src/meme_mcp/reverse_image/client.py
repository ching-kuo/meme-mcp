"""Google Cloud Vision Web Detection client for reverse-image enrichment.

Parses sanitized image bytes into a typed :class:`WebDetectionResult` carrying
raw grounding candidates and a candidate origin, applies a confidence floor, and
NEVER raises into the upload pipeline -- every failure mode maps to a status so
the caller can degrade silently to image-only enrichment (KTD4).

The client deliberately does NOT sanitize web-recovered text: that is the upload
service's single sanitization owner (KTD6). It also never logs image bytes or the
provider's ``error.message`` -- only a redacted status for operator liveness
(KTD10): ``timeout``/``error`` are warnings an operator can alert on, while
``no_match`` is the expected outcome for an unindexed image.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from google.api_core import exceptions as gexc
from google.cloud import vision

logger = logging.getLogger(__name__)

WebDetectionStatus = Literal["success", "low_confidence", "no_match", "timeout", "error"]

# Vision's inline-bytes request caps at 10 MB of JSON. Base64 inflates raw bytes
# by ~4/3, so guard the raw size at ~7.5 MB and skip a call doomed to raise
# InvalidArgument (KTD8). The upload cap is 10 MB raw, so a large re-encoded PNG
# can legitimately exceed this -- treated as no_match (image-only), not a failure.
MAX_INLINE_BYTES = 7_500_000

# Per-call deadline. The lookup runs serially before the ~60s VLM call, so keep
# it tight to protect the combined latency budget (see the plan's latency note).
DEFAULT_TIMEOUT_SECONDS = 8.0

# Confidence floor on the top web-entity relevance score. Vision web-entity
# scores are unbounded relevance values, NOT 0-1 probabilities, so this default
# is a starting point to be CALIBRATED from the real-world efficacy matrix
# (OQ2) before the default-ON path is trusted. Matches at or above the floor
# yield ``success`` (grounding + origin fed with R3 precedence); weaker matches
# yield ``low_confidence`` (origin captured for review, no R3 precedence).
DEFAULT_CONFIDENCE_FLOOR = 0.5

# Drop near-zero entity descriptions before they become grounding noise.
_ENTITY_SCORE_MIN = 0.05


@dataclass(frozen=True)
class WebGrounding:
    """Raw, UNSANITIZED web-recovered context. The service sanitizes before use."""

    best_guess: str
    entities: tuple[str, ...]
    page_titles: tuple[str, ...]


@dataclass(frozen=True)
class OriginCandidate:
    """Raw, UNSANITIZED candidate provenance. The service sanitizes before storage."""

    name: str
    source_url: str


@dataclass(frozen=True)
class WebDetectionResult:
    status: WebDetectionStatus
    grounding: WebGrounding | None
    origin: OriginCandidate | None


class GoogleVisionClient:
    """Thin wrapper over a reused ``ImageAnnotatorClient`` (KTD4).

    Constructed once at app startup from an explicit service-account file (never
    the process-wide ``GOOGLE_APPLICATION_CREDENTIALS`` env var, which would leak
    the credential scope to every other Google client in-process -- U1 warns when
    it is set). The synchronous ``web_detection`` call is wrapped in
    ``asyncio.to_thread`` by the caller (U5).
    """

    def __init__(
        self,
        annotator: Any,
        *,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._annotator = annotator
        self._confidence_floor = confidence_floor
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_credentials_path(
        cls,
        credentials_path: str,
        *,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> GoogleVisionClient:
        annotator = vision.ImageAnnotatorClient.from_service_account_file(credentials_path)
        return cls(
            annotator,
            confidence_floor=confidence_floor,
            timeout_seconds=timeout_seconds,
        )

    def detect(self, image_bytes: bytes) -> WebDetectionResult:
        """Run Web Detection on raw image bytes, returning a never-raising result."""
        if len(image_bytes) > MAX_INLINE_BYTES:
            # Too large for the inline-bytes request; degrade silently to
            # image-only rather than raise InvalidArgument (KTD8). Not a systemic
            # failure, so no liveness warning.
            logger.debug("reverse_image lookup skipped: image exceeds inline-bytes cap")
            return WebDetectionResult("no_match", None, None)
        try:
            response = self._annotator.web_detection(
                image=vision.Image(content=image_bytes),
                timeout=self._timeout_seconds,
            )
        except (gexc.DeadlineExceeded, gexc.RetryError):
            logger.warning("reverse_image lookup failed: timeout")
            return WebDetectionResult("timeout", None, None)
        except gexc.GoogleAPICallError:
            # Redacted on purpose: the exception text can echo request detail and
            # is never surfaced or logged (KTD10).
            logger.warning("reverse_image lookup failed: api_error")
            return WebDetectionResult("error", None, None)
        # A 200 response can still carry an in-body error; consume the message to
        # branch the status but never log or surface it (KTD10).
        if response.error and response.error.message:
            logger.warning("reverse_image lookup failed: response_error")
            return WebDetectionResult("error", None, None)
        return self._parse(response.web_detection)

    def _parse(self, web: Any) -> WebDetectionResult:
        best_guess = _first_label(web.best_guess_labels)
        entities = _entity_descriptions(web.web_entities)
        pages = list(web.pages_with_matching_images)
        page_titles = tuple(p.page_title for p in pages if p.page_title)
        top_page_url = _top_page_url(pages)
        top_score = max((e.score for e in web.web_entities if e.description), default=0.0)

        if not best_guess and not entities and not page_titles:
            return WebDetectionResult("no_match", None, None)

        grounding = WebGrounding(
            best_guess=best_guess,
            entities=entities,
            page_titles=page_titles,
        )
        origin_name = best_guess or (entities[0] if entities else "")
        origin = OriginCandidate(name=origin_name, source_url=top_page_url)
        status: WebDetectionStatus = (
            "success" if top_score >= self._confidence_floor else "low_confidence"
        )
        return WebDetectionResult(status, grounding, origin)


def _first_label(labels: Any) -> str:
    for label in labels:
        if label.label:
            return str(label.label)
    return ""


def _entity_descriptions(entities: Any) -> tuple[str, ...]:
    return tuple(
        str(entity.description)
        for entity in entities
        if entity.description and entity.score >= _ENTITY_SCORE_MIN
    )


def _top_page_url(pages: list[Any]) -> str:
    best = max(pages, key=lambda p: p.score, default=None)
    return str(best.url) if best is not None and best.url else ""
