from __future__ import annotations

from meme_mcp.metadata_locales import localize, merge_locales


def test_localize_prefers_requested_locale_and_falls_back_per_field() -> None:
    metadata = {
        "name": "Distracted Boyfriend",
        "description": "English description",
        "tags": ["english"],
        "locales": {"zh-TW": {"description": "繁體中文描述"}},
    }

    assert localize(metadata, "description", "zh-TW") == "繁體中文描述"
    assert localize(metadata, "name", "zh-TW") == "Distracted Boyfriend"
    assert localize(metadata, "tags", "fr") == ["english"]


def test_merge_locales_preserves_human_and_fills_machine() -> None:
    stored = {
        "name": "English",
        "locales": {
            "zh-TW": {
                "name": "人工名稱",
                "description": "舊描述",
                "_meta": {
                    "name": {"source": "human"},
                    "description": {"source": "machine"},
                },
            }
        },
    }
    incoming = {
        "name": "English",
        "locales": {
            "zh-TW": {
                "name": "機器名稱",
                "description": "新描述",
                "_meta": {
                    "name": {"source": "machine"},
                    "description": {"source": "machine"},
                },
            }
        },
    }

    merged = merge_locales(stored, incoming)

    assert merged["locales"]["zh-TW"]["name"] == "人工名稱"
    assert merged["locales"]["zh-TW"]["description"] == "新描述"


def test_merge_locales_preserves_stored_block_when_incoming_lacks_locales() -> None:
    stored = {"locales": {"zh-TW": {"name": "分心男友"}}}

    assert merge_locales(stored, {"name": "Distracted"})["locales"] == stored["locales"]
