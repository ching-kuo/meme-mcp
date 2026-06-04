from __future__ import annotations

import base64
import json

import itsdangerous
from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from tests.test_upload_flow import good_settings, png_bytes


def _session_cookie(app, login: str) -> str:
    secret = app.state.settings.session_secret.get_secret_value()
    signer = itsdangerous.TimestampSigner(secret)
    data = base64.b64encode(json.dumps({"github_login": login}).encode())
    return signer.sign(data).decode()


def authed_client(tmp_path) -> tuple[TestClient, dict[str, str]]:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("friend")
    image_path = app.state.image_store.put(png_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="deploy-face",
            slug="deploy-face",
            name="Deploy Face",
            source="friend",
            metadata={
                "name": "Deploy Face",
                "description": "deploy relief",
                "emotion": "relief",
                "usage_context": "green CI",
                "tags": ["deploy", "ci"],
                "format": "static",
                "locales": {
                    "zh-TW": {
                        "description": "部署鬆一口氣",
                        "tags": ["部署"],
                        "_meta": {"description": {"source": "machine"}},
                    }
                },
            },
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_browse_renders_authenticated_template_list(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Deploy Face" in response.text


def test_upload_nav_link_absent_for_pat_authed_browse(tmp_path) -> None:
    # A PAT-authenticated /browse caller holds no web session, so the /upload
    # nav link must not render even though friend_login is set.
    client, headers = authed_client(tmp_path)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    assert 'href="/upload"' not in response.text


def test_upload_nav_link_renders_for_allowlisted_session(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    app.state.allowlist.add("friend")
    client = TestClient(app)
    client.cookies.set("session", _session_cookie(app, "friend"))

    response = client.get("/browse")

    assert response.status_code == 200
    assert 'href="/upload"' in response.text


def test_anonymous_browse_redirects_to_login(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    client = TestClient(app)

    # Anonymous /browse is bounced through GitHub login (like /upload), so it
    # renders no page -- and thus no nav -- at all.
    response = client.get("/browse", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/browse"


def test_landing_page_renders_sign_in_for_anonymous(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Anonymous visitor is offered GitHub login, not the app shell. The button
    # targets provider=github explicitly so it bypasses the chooser when Google
    # sign-in is enabled.
    assert 'href="/auth/login?provider=github&next=/browse"' in response.text
    assert 'href="/upload"' not in response.text


def test_landing_page_offers_app_links_for_session(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    app.state.allowlist.add("friend")
    client = TestClient(app)
    client.cookies.set("session", _session_cookie(app, "friend"))

    response = client.get("/")

    assert response.status_code == 200
    assert "friend" in response.text
    assert 'href="/browse"' in response.text
    assert 'href="/upload"' in response.text


def test_browse_grid_references_template_preview_image(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    # The gallery card shows a real preview, served by the template-image route.
    assert "/templates/deploy-face/image" in response.text


def test_browse_card_links_to_detail_page(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/browse", headers=headers)

    assert response.status_code == 200
    # Each gallery card is a link into the template's detail page.
    assert 'href="/templates/deploy-face"' in response.text


def test_template_detail_renders_all_fields(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/templates/deploy-face", headers=headers)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    # Name, id, description, metadata fields, tags, slot, and source all render.
    assert "Deploy Face" in body
    assert "deploy-face" in body
    assert "deploy relief" in body  # description
    assert "relief" in body  # emotion
    assert "green CI" in body  # usage_context
    assert "friend" in body  # source
    for tag in ("deploy", "ci"):
        assert tag in body
    assert "top" in body  # slot name/position
    # The detail page embeds the full-size preview from the image route.
    assert "/templates/deploy-face/image" in body


def test_browse_and_detail_localize_values_with_field_fallback(tmp_path) -> None:
    client, headers = authed_client(tmp_path)
    client.cookies.set("lang", "zh-TW")

    browse = client.get("/browse", headers=headers)
    detail = client.get("/templates/deploy-face", headers=headers)

    assert browse.status_code == 200
    assert "部署鬆一口氣" in browse.text
    assert "部署" in browse.text
    assert "Deploy Face" in browse.text
    assert detail.status_code == 200
    assert "部署鬆一口氣" in detail.text
    assert "relief" in detail.text


def test_template_detail_redirects_anonymous_browser_to_login(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    client = TestClient(app)

    # An anonymous browser is bounced through GitHub login (like /browse), with
    # the shareable detail URL preserved as the post-login return target.
    response = client.get("/templates/deploy-face", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=%2Ftemplates%2Fdeploy-face"


def _upsert_with_origin(app, origin: dict[str, object]) -> str:
    image_path = app.state.image_store.put(png_bytes("white"), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="pigeon",
            slug="pigeon",
            name="Anime Butterfly",
            source="friend",
            metadata={
                "name": "Anime Butterfly",
                "description": "anime man and butterfly",
                "emotion": "wonder",
                "usage_context": "captivated",
                "tags": ["anime"],
                "format": "static",
                "origin": origin,
            },
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="1" * 16,
            exact_hash="b" * 64,
        )
    )
    return "pigeon"


def test_detail_page_renders_origin_with_safe_https_link(tmp_path) -> None:
    client, headers = authed_client(tmp_path)
    template_id = _upsert_with_origin(
        client.app,
        {
            "name": "Is This a Pigeon?",
            "source_url": "https://knowyourmeme.com/memes/is-this-a-pigeon",
            "status": "high",
        },
    )

    body = client.get(f"/templates/{template_id}", headers=headers).text

    assert "Is This a Pigeon?" in body
    assert "https://knowyourmeme.com/memes/is-this-a-pigeon" in body
    assert 'rel="noopener noreferrer"' in body


def test_detail_page_renders_source_panel_for_nameless_origin(tmp_path) -> None:
    # The memegen relocation shape: origin carries only a source_url (no name).
    client, headers = authed_client(tmp_path)
    template_id = _upsert_with_origin(
        client.app, {"source_url": "https://knowyourmeme.com/memes/10-guy"}
    )

    body = client.get(f"/templates/{template_id}", headers=headers).text

    # The provenance link renders even with no origin.name...
    assert "https://knowyourmeme.com/memes/10-guy" in body
    assert 'rel="noopener noreferrer"' in body
    # ...under a "Source" panel heading, never the named "Origin" heading.
    assert '<h2 class="detail__panel-title">Source</h2>' in body
    assert ">Origin<" not in body
    # No name row, and the link row is labeled "Reference" (not a duplicate "Source").
    assert "Identified as" not in body
    assert "<dt>Reference</dt>" in body
    # The corpus tag row is relabeled "Library" so there is no duplicate "Source".
    assert "<dt>Library</dt>" in body


def test_detail_page_does_not_linkify_non_https_source(tmp_path) -> None:
    client, headers = authed_client(tmp_path)
    template_id = _upsert_with_origin(
        client.app,
        {
            "name": "Sneaky",
            "source_url": "javascript:alert(1)",
            "status": "high",
        },
    )

    body = client.get(f"/templates/{template_id}", headers=headers).text

    # The origin name still renders, but the bad URL never becomes a live link.
    assert "Sneaky" in body
    assert 'href="javascript:' not in body


def test_template_detail_invalid_pat_returns_401(tmp_path) -> None:
    client, _ = authed_client(tmp_path)

    response = client.get(
        "/templates/deploy-face", headers={"Authorization": "Bearer nope"}
    )

    assert response.status_code == 401


def test_template_detail_unknown_template_returns_404(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/templates/does-not-exist", headers=headers)

    assert response.status_code == 404


def test_template_image_served_to_authenticated_caller(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/templates/deploy-face/image", headers=headers)

    assert response.status_code == 200
    # Explicit content type (not sniffed) so it renders under nosniff.
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_template_image_requires_auth(tmp_path) -> None:
    client, _ = authed_client(tmp_path)

    response = client.get("/templates/deploy-face/image")

    assert response.status_code == 401


def test_template_image_unknown_template_returns_404(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    response = client.get("/templates/does-not-exist/image", headers=headers)

    assert response.status_code == 404


def test_template_image_missing_blob_returns_404(tmp_path) -> None:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(app.state.pat_store, "friend", app.state.pat_hash_pepper_value)
    app.state.allowlist.add("friend")
    image_path = app.state.image_store.put(png_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="ghost",
            slug="ghost",
            name="Ghost",
            source="friend",
            metadata={"name": "Ghost", "format": "static", "tags": []},
            slot_definitions=[],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="b" * 64,
        )
    )
    # Row survives but its backing blob is gone (e.g. GC'd): expect 404, not 500.
    app.state.image_store.delete(image_path)
    client = TestClient(app)

    response = client.get(
        "/templates/ghost/image", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 404


def test_template_api_searches_and_previews_without_persistence(tmp_path) -> None:
    client, headers = authed_client(tmp_path)

    listed = client.get("/api/templates?q=deploy", headers=headers)
    preview = client.post(
        "/api/templates/deploy-face/preview",
        headers=headers,
        json={"slot_fills": ["ship it"]},
    )

    assert listed.status_code == 200
    assert listed.json()["data"]["templates"][0]["template_id"] == "deploy-face"
    assert preview.status_code == 200
    data_url = preview.json()["data"]["data_url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]).startswith(b"\x89PNG")


# --- U7: provider-aware display + Google sign-in button -----------------------


def _google_app(tmp_path):
    from pydantic import SecretStr

    settings = good_settings(tmp_path).model_copy(
        update={
            "google_oauth_enabled": True,
            "google_client_id": "gid",
            "google_client_secret": SecretStr("gsecret"),
            "google_redirect_uri": "http://localhost:8000/auth/google/callback",
        }
    )
    return create_app(settings)


def test_landing_shows_google_button_only_when_enabled(tmp_path) -> None:
    off = TestClient(create_app(good_settings(tmp_path)))
    assert "/auth/google/login" not in off.get("/").text

    on = TestClient(_google_app(tmp_path))
    assert "/auth/google/login" in on.get("/").text


def test_signed_in_google_friend_sees_gmail_label_not_sub(tmp_path) -> None:
    app = _google_app(tmp_path)
    app.state.pin_store.create_pin("sub-A", "alice@gmail.com")
    app.state.allowlist.add("google:alice@gmail.com")
    client = TestClient(app)
    client.cookies.set("session", _session_cookie(app, "google:sub-A"))
    # The landing page renders "signed in as {label}" for a web session.
    response = client.get("/")
    assert response.status_code == 200
    assert "alice@gmail.com" in response.text
    assert "sub-A" not in response.text


def test_restricted_page_is_provider_neutral_without_operator_handle(tmp_path) -> None:
    # The Google rejection path passes operator_github_login=None; the page must
    # render the generic guidance and never the GitHub-login-specific wording.
    from tests.test_google_oauth_session import FakeGoogleOAuth, _google_settings

    app = create_app(_google_settings(tmp_path))
    app.state.google_oauth = FakeGoogleOAuth(
        sub="s1", email="stranger@gmail.com", email_verified=True
    )
    client = TestClient(app)
    client.get("/auth/google/login", follow_redirects=False)
    response = client.get("/auth/google/callback", follow_redirects=False)
    assert response.status_code == 403
    assert "add your GitHub login" not in response.text
    assert "add you to the allowlist" not in response.text  # the operator-handle suffix
