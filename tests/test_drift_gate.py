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


def test_shared_characters_in_traditional_prose_pass() -> None:
    # Regression: 件 信 台 程 量 息 are valid Traditional characters that also
    # appear inside mainland vocab words. They must not be flagged on their own
    # when hanzidentifier (available here) identifies the text as Traditional.
    assert check_drift("他對這件事很有信心，這是電影台詞").passed
    assert check_drift("匈牙利退休工程師的照片，影片品質很好").passed
    assert check_drift("這個迷因在台灣的網路上很流行").passed


def test_all_shared_char_mainland_vocab_rejects_but_legit_traditional_passes() -> None:
    # Mainland terms made entirely of shared (BOTH) characters slip past the
    # Simplified/Mixed identifier, so the denylist must catch them by whole word.
    assert not check_drift("這個後台程序的后台").passed  # contains 后台 and 程序
    assert not check_drift("程序設計很有趣").passed
    # Legitimate Traditional words built from the same characters must still pass.
    assert check_drift("皇后與工程師按順序入場").passed


def test_mixed_simplified_character_in_traditional_text_rejects() -> None:
    # A genuine Simplified character embedded in Traditional prose -> MIXED.
    assert not check_drift("這是强大的力量").passed  # 强 is Simplified of 強
    assert not check_drift("創意匮乏又重複").passed  # 匮 is Simplified of 匱


def test_charset_fallback_only_when_hanzidentifier_unavailable(monkeypatch) -> None:
    # With hanzidentifier forced unavailable, the conservative charset scan runs
    # and still rejects a genuinely Simplified-only character.
    import builtins
    import sys

    real_import = builtins.__import__

    def _no_hanzi(name, *args, **kwargs):
        if name == "hanzidentifier":
            raise ImportError("forced unavailable")
        return real_import(name, *args, **kwargs)

    # Evict the cached module so the in-function `import hanzidentifier` actually
    # routes through the patched __import__ instead of hitting sys.modules.
    monkeypatch.delitem(sys.modules, "hanzidentifier", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_hanzi)
    # Prove the fallback path is actually taken (no authoritative verdict).
    from meme_mcp.vlm.drift import _hanzidentifier_verdict

    assert _hanzidentifier_verdict("这个软件") is None
    assert not check_drift("这个软件").passed  # simplified chars hit the fallback
    # Shared Traditional chars are excluded from the trimmed fallback set, so
    # valid Traditional prose still passes even on the degraded path.
    assert check_drift("他對這件事很有信心").passed


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
