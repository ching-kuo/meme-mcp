from __future__ import annotations

from meme_mcp.upload.service import _prepare_machine_locales, _stamp_human_locale_edits
from meme_mcp.vlm.drift import check_drift, check_metadata_drift
from meme_mcp.vlm.sanitize import hard_sanitize_metadata


def test_mainland_vocab_and_simplified_characters_reject() -> None:
    assert not check_drift("這個視頻質量很好").passed
    assert not check_drift("这个软件很好").passed


def test_clean_traditional_and_latin_pass() -> None:
    assert check_drift("這個影片品質很好，軟體也好用").passed
    assert check_drift("plain english").passed
    assert check_drift("").passed


def test_metadata_drift_checks_locale_content_only() -> None:
    result = check_metadata_drift(
        {
            "locales": {
                "zh-TW": {
                    "description": "視頻",
                    "_meta": {"description": {"note": "软件"}},
                }
            }
        }
    )

    assert not result.passed
    assert all(reason.startswith("description:") for reason in result.reasons)


def test_metadata_drift_skips_non_string_items() -> None:
    # A malformed pre-sanitize shape must not be repr-coerced into drift text.
    result = check_metadata_drift(
        {"locales": {"zh-TW": {"tags": [{"nested": "視頻"}, 42, "影片"]}}}
    )

    assert result.passed


def test_drift_failure_provenance_survives_sanitize() -> None:
    # AE2/R3: on drift failure the zh-TW content is dropped (English-only) but
    # the per-field drift: "failed" stamp must round-trip hard_sanitize_metadata.
    prepared = _prepare_machine_locales(
        {
            "name": "Deploy Face",
            "description": "english",
            "locales": {"zh-TW": {"description": "這個視頻很好"}},
        }
    )

    assert "description" not in prepared["locales"]["zh-TW"]
    cleaned = hard_sanitize_metadata(prepared)
    assert cleaned["locales"]["zh-TW"]["_meta"]["description"] == {
        "source": "machine",
        "drift": "failed",
    }


def test_stamp_human_locale_edits_marks_changed_fields_only() -> None:
    # Approve-time provenance: only fields the friend actually changed become
    # human; untouched machine values stay machine so backfill can improve them.
    baseline = {
        "locales": {
            "zh-TW": {
                "name": "部署臉",
                "description": "機器描述",
                "_meta": {
                    "name": {"source": "machine", "drift": "pass"},
                    "description": {"source": "machine", "drift": "pass"},
                },
            }
        }
    }
    incoming = {
        "locales": {
            "zh-TW": {
                "name": "部署臉",
                "description": "朋友改寫的描述",
                "_meta": {
                    "name": {"source": "machine", "drift": "pass"},
                    "description": {"source": "machine", "drift": "pass"},
                },
            }
        }
    }

    stamped = _stamp_human_locale_edits(incoming, baseline)

    meta = stamped["locales"]["zh-TW"]["_meta"]
    assert meta["description"] == {"source": "human"}
    assert meta["name"] == {"source": "machine", "drift": "pass"}
    # No locales in the incoming payload is a no-op.
    assert _stamp_human_locale_edits({"name": "X"}, baseline) == {"name": "X"}


def test_drift_pass_stamps_machine_provenance() -> None:
    prepared = _prepare_machine_locales(
        {
            "name": "Deploy Face",
            "locales": {"zh-TW": {"description": "這個影片品質很好"}},
        }
    )

    meta = prepared["locales"]["zh-TW"]["_meta"]
    assert meta["description"] == {"source": "machine", "drift": "pass"}
