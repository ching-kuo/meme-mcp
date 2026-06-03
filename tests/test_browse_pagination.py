from __future__ import annotations

from fastapi.testclient import TestClient

from meme_mcp.app import BROWSE_PAGE_SIZE, _browse_page_url, create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from tests.test_upload_flow import good_settings, png_bytes


def _client_with_templates(tmp_path, count: int) -> tuple[TestClient, dict[str, str]]:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("friend")
    image_path = app.state.image_store.put(png_bytes(), "png")
    for i in range(count):
        # Zero-padded so ORDER BY name matches numeric order, making the page
        # window deterministic.
        ident = f"tmpl-{i:03d}"
        app.state.templates.upsert(
            TemplateCreate(
                template_id=ident,
                slug=ident,
                name=f"Template {i:03d}",
                source="friend",
                metadata={"name": f"Template {i:03d}", "format": "static"},
                slot_definitions=[],
                image_path=image_path,
                perceptual_hash="0" * 16,
                exact_hash=f"{i:064d}",
            )
        )
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _card_count(html: str) -> int:
    return html.count("template-card__img")


def test_no_pager_when_single_page(tmp_path) -> None:
    client, headers = _client_with_templates(tmp_path, BROWSE_PAGE_SIZE)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    assert _card_count(response.text) == BROWSE_PAGE_SIZE
    assert 'class="pager"' not in response.text


def test_first_page_caps_cards_and_renders_pager(tmp_path) -> None:
    client, headers = _client_with_templates(tmp_path, BROWSE_PAGE_SIZE + 5)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    assert _card_count(response.text) == BROWSE_PAGE_SIZE
    assert 'class="pager"' in response.text
    # Masthead count reflects the whole library, not just the page window.
    assert str(BROWSE_PAGE_SIZE + 5) in response.text
    # First page has a next link but no previous link.
    assert 'rel="next"' in response.text
    assert 'rel="prev"' not in response.text
    assert 'href="/browse?page=2"' in response.text


def test_second_page_shows_remainder(tmp_path) -> None:
    client, headers = _client_with_templates(tmp_path, BROWSE_PAGE_SIZE + 5)

    response = client.get("/browse?page=2", headers=headers)

    assert response.status_code == 200
    assert _card_count(response.text) == 5
    # Last page has a previous link but no next link.
    assert 'rel="prev"' in response.text
    assert 'rel="next"' not in response.text
    # First page link drops the page param to stay canonical.
    assert 'href="/browse"' in response.text


def test_out_of_range_page_clamps_to_last(tmp_path) -> None:
    client, headers = _client_with_templates(tmp_path, BROWSE_PAGE_SIZE + 5)

    response = client.get("/browse?page=999", headers=headers)

    assert response.status_code == 200
    # Clamped to the final page (the 5 remaining cards), never a 404.
    assert _card_count(response.text) == 5
    assert 'aria-current="page"' in response.text


def test_zero_or_negative_page_clamps_to_first(tmp_path) -> None:
    client, headers = _client_with_templates(tmp_path, BROWSE_PAGE_SIZE + 5)

    response = client.get("/browse?page=0", headers=headers)

    assert response.status_code == 200
    assert _card_count(response.text) == BROWSE_PAGE_SIZE
    assert 'rel="prev"' not in response.text


def test_non_integer_page_falls_back_to_first(tmp_path) -> None:
    client, headers = _client_with_templates(tmp_path, BROWSE_PAGE_SIZE + 5)

    # A non-numeric ?page (bots, speculative prefetch) renders page 1, never 422.
    response = client.get("/browse?page=foo", headers=headers)

    assert response.status_code == 200
    assert _card_count(response.text) == BROWSE_PAGE_SIZE
    assert 'rel="prev"' not in response.text


def test_browse_page_url_preserves_query() -> None:
    assert _browse_page_url("deploy ci", 1) == "/browse?q=deploy+ci"
    assert _browse_page_url("deploy ci", 3) == "/browse?q=deploy+ci&page=3"
    assert _browse_page_url("", 1) == "/browse"
    assert _browse_page_url("", 2) == "/browse?page=2"
