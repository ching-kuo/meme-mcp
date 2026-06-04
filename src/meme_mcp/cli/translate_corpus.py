from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from openai import OpenAI

from meme_mcp.cli.seed import _default_enrichment_path
from meme_mcp.config import Settings
from meme_mcp.vlm.drift import check_drift

# The four enrichment-asset fields the overlay localizes. `name` stays English by
# design (localize() falls back to the top-level English name); see the module and
# the zh-TW asset `_meta.note` for the documented v1 limitation.
_TRANSLATABLE_PROSE = ("description", "emotion", "usage_context")
_TRANSLATABLE_TAGS = "extra_tags"
_OVERLAY_FIELDS = ("description", "emotion", "usage_context", "tags")

_TRANSLATE_TOOL = {
    "type": "function",
    "function": {
        "name": "record_zh_tw_translation",
        "description": "Record the Traditional Chinese (zh-TW) translation of meme metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "emotion": {"type": "string"},
                "usage_context": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["description", "emotion", "usage_context", "tags"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_INSTRUCTION = (
    "You translate English meme-template metadata into Traditional Chinese as used "
    "in Taiwan (zh-TW). Use Taiwan vocabulary and Traditional characters only -- "
    "never Simplified characters and never mainland-China terms (use 影片 not 視頻, "
    "品質 not 質量, 軟體 not 软件). Preserve meaning and tone. Call the "
    "record_zh_tw_translation tool with the translated fields."
)

_RETRY_SUFFIX = (
    " The previous attempt contained Simplified characters or mainland-China "
    "vocabulary. Re-translate using ONLY Traditional Chinese and Taiwan vocabulary."
)


class TranslationLLM(Protocol):
    """Minimal OpenAI-compatible chat surface the translator drives."""

    @property
    def chat(self) -> Any: ...


def _english_source_fields(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Project one English enrichment entry to the translatable source payload."""
    payload: dict[str, Any] = {
        field: str(entry.get(field, "")) for field in _TRANSLATABLE_PROSE
    }
    tags = [tag for tag in (entry.get(_TRANSLATABLE_TAGS) or []) if isinstance(tag, str)]
    payload["tags"] = tags
    return payload


def _call_llm(
    llm: TranslationLLM, model: str, source: Mapping[str, Any], retry: bool
) -> dict[str, Any]:
    instruction = _SYSTEM_INSTRUCTION + (_RETRY_SUFFIX if retry else "")
    user_content = "Translate this meme metadata to zh-TW:\n" + json.dumps(
        dict(source), ensure_ascii=False, indent=2
    )
    response = llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": user_content},
        ],
        tools=[_TRANSLATE_TOOL],
        tool_choice="required",
        timeout=60,
    )
    raw_args = response.choices[0].message.tool_calls[0].function.arguments
    parsed = json.loads(str(raw_args))
    return parsed if isinstance(parsed, dict) else {}


def _drift_clean_field(value: Any) -> bool:
    """True when every string in the value passes the drift gate."""
    items = value if isinstance(value, list) else [value]
    return all(
        check_drift(item).passed for item in items if isinstance(item, str)
    )


def _translate_slug(
    llm: TranslationLLM,
    model: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate one slug, drift-gating each field with one retry then per-field skip.

    A field that fails the drift gate is retried once via a re-prompt; on a second
    failure it is dropped from the overlay. Drifted text is never written.
    """
    try:
        first = _call_llm(llm, model, source, retry=False)
    except Exception:
        first = {}
    block: dict[str, Any] = {}
    needs_retry = False
    for field in _OVERLAY_FIELDS:
        value = first.get(field)
        if value is None:
            needs_retry = True
            continue
        if _drift_clean_field(value):
            block[field] = value
        else:
            needs_retry = True
    if not needs_retry:
        return block
    try:
        second = _call_llm(llm, model, source, retry=True)
    except Exception:
        return block
    for field in _OVERLAY_FIELDS:
        if field in block:
            continue
        value = second.get(field)
        if value is not None and _drift_clean_field(value):
            block[field] = value
    return block


def generate_overlay(
    llm: TranslationLLM,
    model: str,
    enrichment: Mapping[str, Mapping[str, Any]],
    *,
    existing: Mapping[str, Any] | None = None,
    memegen_commit: str | None = None,
) -> dict[str, Any]:
    """Build the zh-TW overlay artifact from the English enrichment via the LLM.

    Re-generation is stable: a slug whose overlay block already exists is reused
    unchanged (idempotent re-run), so only missing slugs hit the LLM.
    """
    prior = _slug_entries(existing) if isinstance(existing, Mapping) else {}
    overlay: dict[str, Any] = {}
    for slug in sorted(enrichment):
        if slug in prior and prior[slug]:
            overlay[slug] = dict(prior[slug])
            continue
        source = _english_source_fields(enrichment[slug])
        block = _translate_slug(llm, model, source)
        if block:
            overlay[slug] = block
    return {"_meta": _build_meta(model, memegen_commit), **overlay}


def _build_meta(model: str, memegen_commit: str | None) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "model": model,
        "authored": datetime.now(UTC).date().isoformat(),
        "note": (
            "slug -> {description, emotion, usage_context, tags} in Traditional "
            "Chinese (zh-TW, Taiwan vocabulary). Machine-generated zh-TW overlay "
            "applied at import by corpus.upstream, attaching locales['zh-TW'] with "
            "per-field machine provenance and a passing drift status. Each value "
            "passed check_drift at generation (Simplified chars and mainland "
            "vocabulary rejected, retried once, then the field is skipped). `name` "
            "stays English -- localize() falls back to the top-level English name "
            "(documented v1 limitation). Re-running translate-corpus only fills "
            "missing slugs. After applying, run reindex-embeddings --force."
        ),
    }
    if memegen_commit is not None:
        meta["memegen_commit"] = memegen_commit
    return meta


def _slug_entries(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep only slug -> dict entries, dropping the `_meta` header and bad shapes."""
    return {
        slug: value
        for slug, value in data.items()
        if not str(slug).startswith("_") and isinstance(value, dict)
    }


def _load_enrichment_for_translate(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _slug_entries(data) if isinstance(data, dict) else {}


def _load_existing_overlay(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _memegen_commit(enrichment_path: Path) -> str | None:
    try:
        data = json.loads(enrichment_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    meta = data.get("_meta") if isinstance(data, dict) else None
    commit = meta.get("memegen_commit") if isinstance(meta, dict) else None
    return commit if isinstance(commit, str) else None


def _default_output_path() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    return project_root / "assets" / "memegen-enrichment.zh-TW.json"


def run(
    settings: Settings,
    *,
    enrichment_path: Path | None = None,
    output_path: Path | None = None,
    llm: TranslationLLM | None = None,
    model: str | None = None,
) -> int:
    """Generate the committed zh-TW overlay artifact from the English enrichment.

    Reads the English enrichment, translates the four scoped fields per slug via an
    injectable LLM client (default = the project's OpenAI-compatible VLM endpoint),
    drift-gates each value with one retry then per-field skip, and writes the
    overlay. Prints a completion sentinel and a reindex reminder.
    """
    source = enrichment_path or _default_enrichment_path()
    target = output_path or _default_output_path()
    resolved_model = model or settings.vlm_model
    if llm is not None:
        client: TranslationLLM = llm
    else:
        client = OpenAI(
            api_key=settings.vlm_api_key.get_secret_value(),
            base_url=settings.vlm_base_url,
        )

    enrichment = _load_enrichment_for_translate(source)
    existing = _load_existing_overlay(target)
    overlay = generate_overlay(
        client,
        resolved_model,
        enrichment,
        existing=existing,
        memegen_commit=_memegen_commit(source),
    )

    translated = sum(1 for slug in overlay if not slug.startswith("_"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"generated {translated}/{len(enrichment)} slugs to {target}")
    print("run `meme-mcp reindex-embeddings --force` after re-seeding with the overlay")
    return 0
