from __future__ import annotations

import math

from meme_mcp.embeddings.client import EmbeddingClient, embedding_text, embedding_text_hash


def test_embedding_text_includes_localized_values_but_not_names() -> None:
    metadata = {
        "name": "Distracted Boyfriend",
        "description": "tempted by another choice",
        "emotion": "temptation",
        "usage_context": "bad priorities",
        "tags": ["choice"],
        "locales": {
            "zh-TW": {
                "name": "分心男友",
                "description": "注意力被別的事物吸引",
                "emotion": "心動",
                "usage_context": "拿來吐槽優先順序",
                "tags": ["梗圖"],
            }
        },
    }

    text = embedding_text(metadata)

    assert "注意力被別的事物吸引" in text
    assert "梗圖" in text
    assert "Distracted Boyfriend" not in text
    assert "分心男友" not in text


def test_embedding_hash_changes_when_only_locale_changes() -> None:
    original = {
        "description": "same",
        "emotion": "same",
        "usage_context": "same",
        "tags": [],
        "locales": {"zh-TW": {"description": "舊描述"}},
    }
    changed = {
        **original,
        "locales": {"zh-TW": {"description": "新描述"}},
    }

    assert embedding_text_hash(original) != embedding_text_hash(changed)


class _FakeEmbeddings:
    def create(self, **kwargs):
        class _Response:
            class _Item:
                embedding = [3.0, 4.0]

            data = [_Item()]

        return _Response()


class _FakeProvider:
    embeddings = _FakeEmbeddings()


def test_embed_template_and_query_are_l2_normalized() -> None:
    client = EmbeddingClient(model="fake", provider=_FakeProvider())

    for vector in (client.embed_template({}), client.embed_query("難過")):
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0)
