from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

SUPPORTED_CONTENT_LOCALES = frozenset({"zh-TW"})
LOCALIZED_FIELDS = ("name", "description", "emotion", "usage_context", "tags")
ProvenanceSource = Literal["human", "machine"]
DriftStatus = Literal["pass", "failed"]


def localize(metadata: dict[str, Any], field: str, locale: str | None) -> Any:
    """Resolve one metadata field with requested-locale then English fallback."""
    if locale in SUPPORTED_CONTENT_LOCALES:
        localized = _locale_block(metadata, locale)
        if isinstance(localized, dict) and _has_value(localized.get(field)):
            return localized[field]
    return metadata.get(field)


def localized_metadata(metadata: dict[str, Any], locale: str | None) -> dict[str, Any]:
    """Return a display copy with localized user-facing fields overlaid."""
    copy = deepcopy(metadata)
    for field in LOCALIZED_FIELDS:
        value = localize(metadata, field, locale)
        if value is not None:
            copy[field] = value
    return copy


def english_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Project metadata to the canonical English agent-facing shape."""
    copy = deepcopy(metadata)
    copy.pop("locales", None)
    return copy


def provenance(
    source: ProvenanceSource,
    *,
    drift: DriftStatus | None = None,
) -> dict[str, str]:
    entry: dict[str, str] = {"source": source}
    if drift is not None:
        entry["drift"] = drift
    return entry


def stamp_locale_provenance(
    metadata: dict[str, Any],
    locale: str,
    fields: list[str] | tuple[str, ...],
    source: ProvenanceSource,
    *,
    drift: DriftStatus | None = None,
) -> dict[str, Any]:
    """Return a copy with per-field provenance for an existing locale block."""
    copy = deepcopy(metadata)
    if locale not in SUPPORTED_CONTENT_LOCALES:
        return copy
    locales = copy.setdefault("locales", {})
    if not isinstance(locales, dict):
        locales = {}
        copy["locales"] = locales
    block = locales.setdefault(locale, {})
    if not isinstance(block, dict):
        block = {}
        locales[locale] = block
    meta = block.setdefault("_meta", {})
    if not isinstance(meta, dict):
        meta = {}
        block["_meta"] = meta
    for field in fields:
        if field in LOCALIZED_FIELDS and _has_value(block.get(field)):
            meta[field] = provenance(source, drift=drift)
    return copy


def merge_locales(
    stored: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge locale blocks while preserving human-authored values.

    Incoming top-level English remains authoritative for normal approve/import
    paths. Locale fields follow human-wins/machine-fills: stored human values are
    not overwritten by incoming machine values; missing incoming locales preserve
    stored locale blocks.
    """
    if not stored:
        return deepcopy(incoming)

    merged = deepcopy(incoming)
    stored_locales = stored.get("locales")
    incoming_locales = incoming.get("locales")
    if not isinstance(stored_locales, dict):
        return merged
    if not isinstance(incoming_locales, dict):
        merged["locales"] = deepcopy(stored_locales)
        return merged

    result_locales = deepcopy(stored_locales)
    for locale, incoming_block_raw in incoming_locales.items():
        if locale not in SUPPORTED_CONTENT_LOCALES or not isinstance(incoming_block_raw, dict):
            continue
        # result_locales is already a deepcopy of stored_locales, so result_block
        # is private to this call and can be mutated in place.
        result_block = result_locales.get(locale)
        if not isinstance(result_block, dict):
            result_locales[locale] = deepcopy(incoming_block_raw)
            continue
        for field in LOCALIZED_FIELDS:
            if field not in incoming_block_raw:
                continue
            incoming_source = _field_source(incoming_block_raw, field)
            stored_source = _field_source(result_block, field)
            if stored_source == "human" and incoming_source != "human":
                continue
            result_block[field] = deepcopy(incoming_block_raw[field])
        incoming_meta = incoming_block_raw.get("_meta")
        if isinstance(incoming_meta, dict):
            stored_meta = result_block.get("_meta")
            result_meta = deepcopy(stored_meta) if isinstance(stored_meta, dict) else {}
            for field, entry in incoming_meta.items():
                if field not in LOCALIZED_FIELDS or not isinstance(entry, dict):
                    continue
                if _field_source(result_block, field) == "human" and entry.get("source") != "human":
                    continue
                result_meta[field] = deepcopy(entry)
            if result_meta:
                result_block["_meta"] = result_meta
    merged["locales"] = result_locales
    return merged


def _locale_block(metadata: dict[str, Any], locale: str | None) -> dict[str, Any] | None:
    locales = metadata.get("locales")
    if not isinstance(locales, dict) or locale is None:
        return None
    block = locales.get(locale)
    return block if isinstance(block, dict) else None


def _field_source(block: dict[str, Any], field: str) -> str | None:
    meta = block.get("_meta")
    if not isinstance(meta, dict):
        return None
    entry = meta.get(field)
    if not isinstance(entry, dict):
        return None
    source = entry.get("source")
    return source if isinstance(source, str) else None


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return True
