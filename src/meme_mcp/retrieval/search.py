from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

OUTCOME_BOOST_PER_USE = 0.05
OUTCOME_BOOST_CAP = 0.20


@dataclass(frozen=True)
class TemplateRecord:
    template_id: str
    slug: str
    name: str
    metadata: dict[str, Any]
    slot_definitions: list[dict[str, Any]]


@dataclass(frozen=True)
class Candidate:
    template_id: str
    slug: str
    name: str
    similarity_score: float
    matched_fields: list[str]
    slot_definitions: list[dict[str, Any]]
    suggested_slot_fills: list[str]
    metadata: dict[str, Any]


def _flatten(metadata: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in metadata.items():
        # The origin block is provenance, not descriptive text: its name surfaces
        # only via the confidence-gated alias below, and source_url is a URL, not
        # a search term -- so neither pollutes term scoring (U7/KTD9).
        if key == "origin":
            continue
        if isinstance(value, dict):
            parts.append(_flatten(value))
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def _get_dotted(data: dict[str, Any], key: str) -> Any:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


NAME_MATCH_RATIO = 0.72


def _fuzzy_match(needle: str, value: str) -> bool:
    return needle in value or SequenceMatcher(None, needle, value).ratio() >= NAME_MATCH_RATIO


def _name_match(query: str, record: TemplateRecord) -> bool:
    needle = query.lower()
    return any(
        _fuzzy_match(needle, value) for value in (record.slug.lower(), record.name.lower())
    )


def _origin_name_match(query: str, record: TemplateRecord) -> bool:
    """Match the web-recovered origin name, gated on persisted high confidence.

    Only a high-confidence (or friend-confirmed) origin earns the alias bonus
    (KTD9): a low-confidence, unreviewed origin name must not become a
    high-weight retrieval alias. ``origin.status`` is read from the stored blob
    because the runtime ``WebDetectionResult.status`` does not survive to query
    time.
    """
    if _get_dotted(record.metadata, "origin.status") != "high":
        return False
    origin_name = _get_dotted(record.metadata, "origin.name")
    if not isinstance(origin_name, str) or not origin_name.strip():
        return False
    return _fuzzy_match(query.lower(), origin_name.lower())


def search(
    records: list[TemplateRecord],
    query: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
    outcome_lookup: Callable[[str], int] | None = None,
) -> list[Candidate]:
    filters = filters or {}
    query_terms = {term for term in query.lower().split() if term}
    candidates: list[Candidate] = []
    for record in records:
        if any(_get_dotted(record.metadata, key) != value for key, value in filters.items()):
            continue
        haystack = _flatten(record.metadata)
        matched_terms = {term for term in query_terms if term in haystack}
        matched_fields = [key for key in filters]
        score = len(matched_terms) / max(len(query_terms), 1)
        if _name_match(query, record):
            score += 10.0
            matched_fields.append("name_match")
        if _origin_name_match(query, record):
            score += 10.0
            matched_fields.append("origin_name_match")
        if outcome_lookup is not None:
            recent = outcome_lookup(record.template_id)
            if recent > 0:
                boost = min(OUTCOME_BOOST_CAP, OUTCOME_BOOST_PER_USE * recent)
                score += boost
                matched_fields.append("outcome_boost")
        if score > 0 or not query_terms:
            candidates.append(
                Candidate(
                    record.template_id,
                    record.slug,
                    record.name,
                    score,
                    matched_fields,
                    record.slot_definitions,
                    [],
                    record.metadata,
                )
            )
    return sorted(candidates, key=lambda item: item.similarity_score, reverse=True)[: min(top_k, 5)]

