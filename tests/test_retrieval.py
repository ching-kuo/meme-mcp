from meme_mcp.retrieval.search import TemplateRecord, search


def records() -> list[TemplateRecord]:
    return [
        TemplateRecord(
            template_id="1",
            slug="drake",
            name="Drake Hotline Bling",
            metadata={
                "description": "Reject one thing and approve another",
                "emotion": "preference",
                "usage_context": "compare two options",
                "tags": ["choice"],
                "format": "static",
            },
            slot_definitions=[],
        ),
        TemplateRecord(
            template_id="2",
            slug="ci-party",
            name="CI Party",
            metadata={
                "description": "celebrate a clean CI run",
                "emotion": "celebration",
                "usage_context": "Rust build passed in continuous integration",
                "tags": ["rust", "ci-pass"],
                "engineering_context": {"language": "rust", "lifecycle_stage": "ci-pass"},
                "format": "static",
            },
            slot_definitions=[],
        ),
    ]


def test_search_filters_and_ranks_semantic_terms() -> None:
    found = search(records(), "celebrate clean CI Rust", {"engineering_context.language": "rust"})
    assert [candidate.template_id for candidate in found] == ["2"]
    assert "engineering_context.language" in found[0].matched_fields


def test_name_match_boosts_to_first() -> None:
    found = search(records(), "drak")
    assert found[0].slug == "drake"
    assert "name_match" in found[0].matched_fields


def test_top_k_is_capped_at_five() -> None:
    many = records() * 10
    assert len(search(many, "ci", top_k=10)) <= 5

