from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from meme_mcp.app import AppMCPBackend, create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.retrieval.search import TemplateRecord, search
from tests.test_upload_flow import good_settings, png_bytes


def _record(origin: dict[str, Any] | None) -> TemplateRecord:
    metadata: dict[str, Any] = {
        "description": "an anime man gesturing at a butterfly",
        "emotion": "wonder",
        "usage_context": "captivated",
        "tags": ["anime"],
        "format": "static",
    }
    if origin is not None:
        metadata["origin"] = origin
    return TemplateRecord(
        template_id="pigeon",
        slug="anime-butterfly",
        name="Anime Butterfly",
        metadata=metadata,
        slot_definitions=[],
    )


def test_high_confidence_origin_name_earns_alias_bonus() -> None:
    record = _record(
        {
            "name": "Is This a Pigeon?",
            "source_url": "https://knowyourmeme.com/memes/is-this-a-pigeon",
            "status": "high",
        }
    )
    found = search([record], "is this a pigeon")
    assert found[0].template_id == "pigeon"
    assert "origin_name_match" in found[0].matched_fields
    assert found[0].similarity_score >= 10.0


def test_low_confidence_origin_name_earns_no_bonus() -> None:
    record = _record(
        {"name": "Is This a Pigeon?", "source_url": "", "status": "low"}
    )
    found = search([record], "is this a pigeon")
    # The display name/slug do not match the query, and a low-status origin name
    # is not an alias, so the template is not surfaced by name.
    assert not found or "origin_name_match" not in found[0].matched_fields


def test_empty_origin_block_leaves_matching_unchanged() -> None:
    # No false-positive against an empty origin name for a short query.
    record = _record({"name": "", "source_url": "", "status": "high"})
    found = search([record], "is")
    assert not found or "origin_name_match" not in found[0].matched_fields


def test_no_origin_block_is_safe() -> None:
    found = search([_record(None)], "anime")
    assert found[0].template_id == "pigeon"
    assert "origin_name_match" not in found[0].matched_fields


def test_source_url_tokens_do_not_contribute_to_term_scoring() -> None:
    record = _record(
        {
            "name": "Distracted Boyfriend",
            "source_url": "https://example.com/knowyourmeme-distracted",
            "status": "high",
        }
    )
    # "knowyourmeme" appears only inside source_url; it must not match as a term.
    found = search([record], "knowyourmeme")
    assert not found


def test_provenance_only_origin_is_not_a_search_alias() -> None:
    # The memegen relocation shape: origin holds only a source_url (no name/status).
    record = _record({"source_url": "https://knowyourmeme.com/memes/10-guy"})
    # The host token lives only in source_url, which _flatten excludes -> no match.
    assert not search([record], "knowyourmeme")
    # Descriptive fields still match, and a name-less origin earns no alias bonus.
    found = search([record], "anime")
    assert found and "origin_name_match" not in found[0].matched_fields


# ---------------------------------------------------------------------------
# AE5: the origin_name_match tag survives serialization to the find envelope
# ---------------------------------------------------------------------------


def _seed_pigeon(app: Any) -> None:
    image_path = app.state.image_store.put(png_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="pigeon",
            slug="anime-butterfly",
            name="Anime Butterfly",
            source="friend",
            metadata={
                "name": "Anime Butterfly",
                "description": "anime man and a butterfly",
                "emotion": "wonder",
                "usage_context": "captivated",
                "tags": ["anime"],
                "format": "static",
                "origin": {
                    "name": "Is This a Pigeon?",
                    "source_url": "https://knowyourmeme.com/memes/is-this-a-pigeon",
                    "status": "high",
                },
            },
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )


def test_origin_name_match_reaches_find_envelope(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    app.state.allowlist.add("friend")
    _seed_pigeon(app)

    envelope = AppMCPBackend(app).find("is this a pigeon", None, "friend")

    candidates = envelope["data"]["candidates"]
    pigeon = next(c for c in candidates if c["template_id"] == "pigeon")
    # The tag survives the Candidate->envelope serialization path (not just the
    # in-memory search result).
    assert "origin_name_match" in pigeon["matched_fields"]


def test_origin_name_match_reaches_http_find_endpoint(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("friend")
    _seed_pigeon(app)
    client = TestClient(app)

    response = client.post(
        "/api/mcp/find",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "is this a pigeon"},
    )

    assert response.status_code == 200
    candidates = response.json()["data"]["candidates"]
    pigeon = next(c for c in candidates if c["template_id"] == "pigeon")
    assert "origin_name_match" in pigeon["matched_fields"]
