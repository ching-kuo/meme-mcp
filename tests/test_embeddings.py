from meme_mcp.embeddings.client import EmbeddingClient, embedding_text, embedding_text_hash


class FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create(self, *, model: str, input: list[str]):
        self.calls.append((model, input[0]))

        class Item:
            embedding = [0.1, 0.2, 0.3]

        class Response:
            data = [Item()]

        return Response()


class FakeOpenAI:
    def __init__(self) -> None:
        self.embeddings = FakeEmbeddings()


def test_embedding_text_excludes_format_and_sorts_tags() -> None:
    left = {
        "description": "d",
        "emotion": "e",
        "usage_context": "u",
        "tags": ["b", "a"],
        "format": "static",
    }
    right = {**left, "tags": ["a", "b"], "format": "gif"}
    assert embedding_text(left) == embedding_text(right)
    assert "static" not in embedding_text(left)
    assert embedding_text_hash(left) == embedding_text_hash(right)


def test_embedding_text_includes_enriched_prose_and_excludes_origin_url() -> None:
    metadata = {
        "name": "10 Guy",
        "description": "stating something obviously true",
        "emotion": "smug",
        "usage_context": "reacting to an obvious fact",
        "tags": ["ten", "guy"],
        "format": "static",
        "origin": {"source_url": "https://knowyourmeme.com/memes/10-guy"},
    }
    text = embedding_text(metadata)
    assert "obviously true" in text
    # origin is never read by embedding_text, so the URL host never embeds.
    assert "knowyourmeme" not in text


def test_embedding_text_degrades_to_tags_only_when_unenriched() -> None:
    # An un-enriched memegen row (empty prose) still embeds its keyword tags.
    metadata = {
        "name": "10 Guy",
        "description": "",
        "emotion": "",
        "usage_context": "",
        "tags": ["ten", "guy"],
        "format": "static",
        "origin": {"source_url": "https://knowyourmeme.com/memes/10-guy"},
    }
    text = embedding_text(metadata)
    assert "knowyourmeme" not in text
    assert "ten" in text and "guy" in text


def test_embedding_client_uses_provider_and_model() -> None:
    fake = FakeOpenAI()
    client = EmbeddingClient(model="text-embedding-3-small", provider=fake)
    vector = client.embed_template(
        {"description": "hello", "emotion": "joy", "usage_context": "tests"}
    )
    assert vector == [0.1, 0.2, 0.3]
    assert fake.embeddings.calls[0][0] == "text-embedding-3-small"
