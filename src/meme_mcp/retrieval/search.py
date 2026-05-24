from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


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
    for value in metadata.values():
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


def _name_match(query: str, record: TemplateRecord) -> bool:
    needle = query.lower()
    return any(
        needle in value
        or SequenceMatcher(None, needle, value).ratio() >= 0.72
        for value in (record.slug.lower(), record.name.lower())
    )


def search(
    records: list[TemplateRecord],
    query: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
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

