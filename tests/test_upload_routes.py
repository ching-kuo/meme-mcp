"""Route-level locale plumbing for the upload front doors (U4).

The web ``/upload/analyze`` route resolves the author's UI locale from the
request (lang cookie -> Accept-Language -> "en") and passes it into
``analyze_image``; the PAT ``/api/uploads/analyze`` route defaults to "en". The
service-level bilingual/drift behavior is covered in test_upload_flow; here we
pin only that the routes thread the correct locale through.
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

import meme_mcp.app as app_module
import meme_mcp.web.upload_routes as upload_routes_module
from meme_mcp.upload.service import AnalyzeResult
from tests.test_upload_flow import FakeVLMClient, auth_headers, good_settings, png_bytes
from tests.test_web_upload import _csrf_headers, _make_app, _session_client


class _LocaleSpy:
    """Replacement for analyze_image that records the locale it was called with."""

    def __init__(self) -> None:
        self.locale: str | None = None

    async def __call__(self, **kwargs: Any) -> AnalyzeResult:
        self.locale = kwargs.get("locale")
        return AnalyzeResult(
            pending_upload_id="pending-1",
            metadata={"name": "Deploy Face"},
            slot_definitions=[{"name": "top", "position": "top"}],
            duplicate_action="accept",
            duplicate_template_id=None,
            suspect_flags=[],
            reverse_image_status="skipped",
        )


def _analyze_payload() -> dict[str, Any]:
    return {
        "filename": "deploy.png",
        "mime": "image/png",
        "content_base64": base64.b64encode(png_bytes()).decode(),
        "title_hint": "Deploy Face",
    }


def test_web_route_passes_zh_tw_cookie_locale(monkeypatch, tmp_path) -> None:
    app = _make_app(tmp_path)
    spy = _LocaleSpy()
    monkeypatch.setattr(upload_routes_module, "analyze_image", spy)
    client = _session_client(app)
    client.cookies.set("lang", "zh-TW")

    response = client.post("/upload/analyze", headers=_csrf_headers(), json=_analyze_payload())

    assert response.status_code == 200
    assert spy.locale == "zh-TW"


def test_web_route_defaults_to_en_without_cookie(monkeypatch, tmp_path) -> None:
    app = _make_app(tmp_path)
    spy = _LocaleSpy()
    monkeypatch.setattr(upload_routes_module, "analyze_image", spy)
    client = _session_client(app)

    response = client.post("/upload/analyze", headers=_csrf_headers(), json=_analyze_payload())

    assert response.status_code == 200
    assert spy.locale == "en"


def test_web_route_negotiates_accept_language(monkeypatch, tmp_path) -> None:
    app = _make_app(tmp_path)
    spy = _LocaleSpy()
    monkeypatch.setattr(upload_routes_module, "analyze_image", spy)
    client = _session_client(app)

    response = client.post(
        "/upload/analyze",
        headers={**_csrf_headers(), "Accept-Language": "zh-Hant,zh;q=0.9"},
        json=_analyze_payload(),
    )

    assert response.status_code == 200
    assert spy.locale == "zh-TW"


def test_pat_route_defaults_to_en(monkeypatch, tmp_path) -> None:
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    spy = _LocaleSpy()
    monkeypatch.setattr(app_module, "analyze_image", spy)
    client = TestClient(app)

    response = client.post(
        "/api/uploads/analyze",
        headers={**auth_headers(client), "Accept-Language": "zh-TW"},
        json=_analyze_payload(),
    )

    # The PAT door has no UI locale: it always reviews the English proposal,
    # regardless of Accept-Language.
    assert response.status_code == 200
    assert spy.locale == "en"


def test_pat_analyze_unaffected_default_end_to_end(tmp_path) -> None:
    # Without the spy: the real PAT analyze still produces an English proposal.
    from meme_mcp.app import create_app

    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    client = TestClient(app)

    response = client.post(
        "/api/uploads/analyze", headers=auth_headers(client), json=_analyze_payload()
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["metadata"]["name"] == "Deploy Face"
    assert "locales" not in body["metadata"]
