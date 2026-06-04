from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from meme_mcp.metadata_locales import english_metadata

OUTCOME_BOOST_PER_USE = 0.05
OUTCOME_BOOST_CAP = 0.20
CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)


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
        if key == "origin" or key.startswith("_"):
            continue
        if isinstance(value, dict):
            parts.append(_flatten(value))
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def project_candidate_english(candidate: Candidate) -> dict[str, Any]:
    data = candidate.__dict__.copy()
    data["metadata"] = english_metadata(candidate.metadata)
    return data


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


def _contains_cjk(text: str) -> bool:
    return any(any(start <= ord(char) <= end for start, end in CJK_RANGES) for char in text)


def _cjk_tokens(text: str) -> set[str]:
    chars = [char for char in text.lower() if _contains_cjk(char)]
    tokens = set(chars)
    tokens.update("".join(pair) for pair in zip(chars, chars[1:], strict=False))
    return tokens


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
    raw_terms = {term for term in query.lower().split() if term}
    # Route by codepoint (U6): CJK-bearing terms score through the bigram path
    # below, never the substring path -- a one-char substring hit would otherwise
    # full-score every haystack containing that character.
    query_terms = {term for term in raw_terms if not _contains_cjk(term)}
    query_has_cjk = _contains_cjk(query)
    query_cjk_tokens = _cjk_tokens(query) if query_has_cjk else set()
    candidates: list[Candidate] = []
    for record in records:
        if any(_get_dotted(record.metadata, key) != value for key, value in filters.items()):
            continue
        haystack = _flatten(record.metadata)
        matched_terms = {term for term in query_terms if term in haystack}
        matched_fields = [key for key in filters]
        score = len(matched_terms) / max(len(query_terms), 1)
        if query_has_cjk and query_cjk_tokens:
            haystack_tokens = _cjk_tokens(haystack)
            overlap = query_cjk_tokens & haystack_tokens
            # Bigrams are the unit of CJK lexical matching: a multi-char query
            # scores only on bigram overlap so scattered single-character hits
            # cannot flood the candidate set, and a single-char query (which has
            # no bigrams) gets a damped flat boost rather than a full-score match
            # against every haystack containing that character.
            query_bigrams = {token for token in query_cjk_tokens if len(token) > 1}
            if query_bigrams:
                bigram_overlap = {token for token in overlap if len(token) > 1}
                if bigram_overlap:
                    score += len(bigram_overlap) / len(query_bigrams)
                    matched_fields.append("cjk_lexical")
            elif overlap:
                score += 0.5
                matched_fields.append("cjk_lexical")
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
        if score > 0 or not raw_terms:
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
