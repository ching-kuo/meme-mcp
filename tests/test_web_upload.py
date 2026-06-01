"""Session-authed web upload endpoints (U7).

Covers AE10 (PAT/session parity), AE6 (cross-friend owner-scoping), AE7
(rate-limit), AE9 (CSRF), AE3/AE4 (duplicate handling), the unauthenticated
path, the pre-buffer body-size guard, and the discard row-only/blob-kept rule.

Sessions are seated directly by signing the Starlette session cookie with the
app's session secret (the same itsdangerous TimestampSigner the middleware
uses). This is the U8 ``GET /upload`` page's job at runtime, but U7 ships
without that page, so the tests mint the cookie themselves to exercise the
endpoints in isolation.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import itsdangerous
from fastapi.testclient import TestClient

from meme_mcp.app import MAX_ANALYZE_BODY_BYTES, BodySizeGuardMiddleware, create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from meme_mcp.limits import WindowedRateLimiter
from meme_mcp.upload.strip import strip_and_reencode
from meme_mcp.upload.validation import compute_hashes
from meme_mcp.web.csrf import CSRF_HEADER_NAME
from tests.test_upload_flow import (
    FakeReverseImageClient,
    FakeVLMClient,
    _pigeon_success,
    good_settings,
    png_bytes,
)

CSRF_TOKEN = "test-csrf-token-value"


def _make_app(tmp_path, *, logins: tuple[str, ...] = ("friend",)):
    tmp_path.mkdir(parents=True, exist_ok=True)
    app = create_app(good_settings(tmp_path))
    app.state.vlm_client = FakeVLMClient()
    for login in logins:
        app.state.allowlist.add(login)
    return app


def _session_cookie(app, login: str, *, csrf: str | None = CSRF_TOKEN) -> str:
    """Sign a Starlette session cookie carrying a login and CSRF token."""
    payload: dict[str, str] = {"github_login": login}
    if csrf is not None:
        payload["csrf_token"] = csrf
    secret = app.state.settings.session_secret.get_secret_value()
    signer = itsdangerous.TimestampSigner(secret)
    data = base64.b64encode(json.dumps(payload).encode())
    return signer.sign(data).decode()


def _session_client(app, login: str = "friend", *, csrf: str | None = CSRF_TOKEN) -> TestClient:
    client = TestClient(app)
    client.cookies.set("session", _session_cookie(app, login, csrf=csrf))
    return client


def _csrf_headers(token: str = CSRF_TOKEN) -> dict[str, str]:
    return {CSRF_HEADER_NAME: token}


def _analyze_body(image: bytes | None = None, *, title_hint: str = "Deploy Face") -> dict[str, Any]:
    content = image if image is not None else png_bytes()
    return {
        "filename": "deploy.png",
        "mime": "image/png",
        "content_base64": base64.b64encode(content).decode(),
        "title_hint": title_hint,
    }


# ---------------------------------------------------------------------------
# AE10: PAT/session parity
# ---------------------------------------------------------------------------


def test_session_analyze_and_approve_matches_pat_template(tmp_path) -> None:
    image = png_bytes("white")

    # PAT path: a write-capable PAT friend produces a template for the image.
    pat_app = _make_app(tmp_path / "pat", logins=("patfriend",))
    pat_token = issue_pat(pat_app.state.pat_store, "patfriend", pat_app.state.pat_hash_pepper_value)
    pat_client = TestClient(pat_app)
    pat_headers = {"Authorization": f"Bearer {pat_token}"}
    pat_analyzed = pat_client.post(
        "/api/uploads/analyze", headers=pat_headers, json=_analyze_body(image)
    ).json()["data"]
    pat_template_id = pat_client.post(
        f"/api/uploads/{pat_analyzed['pending_upload_id']}/approve",
        headers=pat_headers,
        json={"metadata": pat_analyzed["metadata"]},
    ).json()["data"]["template_id"]

    # Session path: a non-operator allowlisted friend (no PAT) does the same.
    web_app = _make_app(tmp_path / "web", logins=("webfriend",))
    web_client = _session_client(web_app, "webfriend")
    analyzed = web_client.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body(image)
    )
    assert analyzed.status_code == 200
    analyzed_data = analyzed.json()["data"]
    approved = web_client.post(
        f"/upload/approve/{analyzed_data['pending_upload_id']}",
        headers=_csrf_headers(),
        json={"metadata": analyzed_data["metadata"]},
    )
    assert approved.status_code == 200
    web_template_id = approved.json()["data"]["template_id"]

    # The template identity (id derived from name + exact content hash) matches.
    assert web_template_id == pat_template_id
    web_template = web_app.state.templates.get(web_template_id)
    assert web_template.source == "friend"
    assert web_template.name == "Deploy Face"


# ---------------------------------------------------------------------------
# AE2 (web half): the web door defaults identify_online ON
# ---------------------------------------------------------------------------


def test_web_door_defaults_identify_online_on(tmp_path) -> None:
    # No identify_online field on the web door means the lookup runs (the
    # interactive surface defaults ON, KTD7).
    app = _make_app(tmp_path)
    reverse = FakeReverseImageClient(_pigeon_success())
    app.state.reverse_image_client = reverse
    client = _session_client(app)

    data = client.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]

    assert reverse.calls  # lookup ran without an explicit toggle
    assert data["reverse_image_status"] == "success"
    assert data["metadata"]["origin"]["name"] == "Is This a Pigeon?"


def test_web_door_unchecked_toggle_suppresses_egress(tmp_path) -> None:
    app = _make_app(tmp_path)
    reverse = FakeReverseImageClient(_pigeon_success())
    app.state.reverse_image_client = reverse
    client = _session_client(app)

    body = {**_analyze_body(), "identify_online": False}
    data = client.post("/upload/analyze", headers=_csrf_headers(), json=body).json()["data"]

    assert reverse.calls == []  # unchecked toggle == no egress
    assert data["reverse_image_status"] == "skipped"


# ---------------------------------------------------------------------------
# AE6: cross-friend owner-scoping
# ---------------------------------------------------------------------------


def test_friend_b_cannot_approve_friend_a_pending(tmp_path) -> None:
    app = _make_app(tmp_path, logins=("friend_a", "friend_b"))
    client_a = _session_client(app, "friend_a")
    analyzed = client_a.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]
    upload_id = analyzed["pending_upload_id"]

    client_b = _session_client(app, "friend_b")
    response = client_b.post(
        f"/upload/approve/{upload_id}",
        headers=_csrf_headers(),
        json={"metadata": analyzed["metadata"]},
    )

    # Opaque NOT_FOUND -- never reveal that the id exists for friend_a.
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"
    # No template upserted; friend_a's pending row survives untouched.
    assert app.state.templates.list_rows() == []
    assert app.state.pending_uploads.get(upload_id, "friend_a").upload_id == upload_id


def test_friend_b_discard_of_friend_a_pending_changes_nothing(tmp_path) -> None:
    app = _make_app(tmp_path, logins=("friend_a", "friend_b"))
    client_a = _session_client(app, "friend_a")
    analyzed = client_a.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]
    upload_id = analyzed["pending_upload_id"]
    image_path = app.state.pending_uploads.get(upload_id, "friend_a").image_path

    client_b = _session_client(app, "friend_b")
    response = client_b.post(f"/upload/discard/{upload_id}", headers=_csrf_headers())

    # Discard is opaquely successful even though friend_b owns nothing.
    assert response.status_code == 200
    assert response.json()["data"]["discarded"] is True
    # friend_a's row and its blob are both untouched.
    assert app.state.pending_uploads.get(upload_id, "friend_a").upload_id == upload_id
    assert app.state.image_store.get(image_path)


# ---------------------------------------------------------------------------
# AE7: rate limiting
# ---------------------------------------------------------------------------


def test_analyze_rate_limited_stores_nothing(tmp_path) -> None:
    app = _make_app(tmp_path)
    # A zero-limit window so the very first hit exceeds, charged before decode.
    app.state.upload_limiter = WindowedRateLimiter(0, 3600)
    client = _session_client(app)

    response = client.post("/upload/analyze", headers=_csrf_headers(), json=_analyze_body())

    assert response.status_code == 429
    assert response.json()["error_code"] == "RATE_LIMITED"
    # Nothing was persisted: the limiter fires before the row is created, so no live
    # pending row exists (expired() can never observe a fresh 24h-TTL row), and no
    # template.
    assert app.state.pending_uploads.live_image_paths() == set()
    assert app.state.templates.list_rows() == []


# ---------------------------------------------------------------------------
# AE9: CSRF enforcement on all three endpoints
# ---------------------------------------------------------------------------


def test_analyze_rejected_without_csrf_header(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)

    response = client.post("/upload/analyze", json=_analyze_body())

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    assert app.state.templates.list_rows() == []


def test_approve_rejected_with_wrong_csrf_header(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)
    analyzed = client.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]

    response = client.post(
        f"/upload/approve/{analyzed['pending_upload_id']}",
        headers=_csrf_headers("wrong-token"),
        json={"metadata": analyzed["metadata"]},
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    assert app.state.templates.list_rows() == []


def test_discard_rejected_without_csrf_header(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)
    analyzed = client.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]
    upload_id = analyzed["pending_upload_id"]

    response = client.post(f"/upload/discard/{upload_id}")

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    # The row survives a CSRF-rejected discard.
    assert app.state.pending_uploads.get(upload_id, "friend").upload_id == upload_id


# ---------------------------------------------------------------------------
# AE3 / AE4: duplicate handling
# ---------------------------------------------------------------------------


def test_analyze_exact_duplicate_returns_409(tmp_path) -> None:
    image = png_bytes()
    exact_hash, perceptual_hash = compute_hashes(strip_and_reencode(image, "image/png"))
    app = _make_app(tmp_path)
    app.state.templates.upsert(
        TemplateCreate(
            template_id="existing",
            slug="existing",
            name="Existing",
            source="friend",
            metadata={"tags": ["deploy"]},
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path="existing.png",
            perceptual_hash=perceptual_hash,
            exact_hash=exact_hash,
        )
    )
    client = _session_client(app)

    response = client.post("/upload/analyze", headers=_csrf_headers(), json=_analyze_body(image))

    assert response.status_code == 409
    assert response.json()["error_code"] == "DUPLICATE_TEMPLATE"
    assert "existing" in json.dumps(response.json()["errors"])


def test_analyze_near_duplicate_warns_without_blocking(tmp_path) -> None:
    image = png_bytes()
    _, perceptual_hash = compute_hashes(strip_and_reencode(image, "image/png"))
    app = _make_app(tmp_path)
    # Same perceptual hash, different exact hash -> near-duplicate "warn".
    app.state.templates.upsert(
        TemplateCreate(
            template_id="nearby",
            slug="nearby",
            name="Nearby",
            source="friend",
            metadata={"tags": ["deploy"]},
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path="nearby.png",
            perceptual_hash=perceptual_hash,
            exact_hash="f" * 64,
        )
    )
    client = _session_client(app)

    response = client.post("/upload/analyze", headers=_csrf_headers(), json=_analyze_body(image))

    assert response.status_code == 200
    duplicate = response.json()["data"]["duplicate"]
    assert duplicate["action"] == "warn"
    assert duplicate["template_id"] == "nearby"
    # The pending row is still created (warn is non-blocking).
    assert response.json()["data"]["pending_upload_id"]


# ---------------------------------------------------------------------------
# Service-error envelopes on the web door (parity with the PAT door)
# ---------------------------------------------------------------------------


def test_analyze_rejects_base64_garbage(tmp_path) -> None:
    # The shared service raises the INVALID_INPUT base64 envelope; the web door must
    # surface it identically to the PAT door (the JS error contract maps it).
    app = _make_app(tmp_path)
    client = _session_client(app)

    body = {
        "filename": "deploy.png",
        "mime": "image/png",
        "content_base64": "not base64!!",
        "title_hint": "Deploy Face",
    }
    response = client.post("/upload/analyze", headers=_csrf_headers(), json=body)

    assert response.status_code == 400
    envelope = response.json()
    assert envelope["error_code"] == "INVALID_INPUT"
    assert envelope["errors"] == [{"field": "content_base64", "reason": "base64"}]
    assert app.state.pending_uploads.live_image_paths() == set()


def test_analyze_rejects_mime_mismatch(tmp_path) -> None:
    # PNG bytes declared as JPEG -> detected-vs-declared mismatch through the service.
    app = _make_app(tmp_path)
    client = _session_client(app)

    body = {
        "filename": "deploy.jpg",
        "mime": "image/jpeg",
        "content_base64": base64.b64encode(png_bytes()).decode(),
        "title_hint": "Deploy Face",
    }
    response = client.post("/upload/analyze", headers=_csrf_headers(), json=body)

    assert response.status_code == 400
    envelope = response.json()
    assert envelope["error_code"] == "UPLOAD_REJECTED"
    assert envelope["errors"] == [{"field": "file", "reason": "mime_mismatch"}]
    assert app.state.pending_uploads.live_image_paths() == set()


# ---------------------------------------------------------------------------
# Unauthenticated and PAT-rejection
# ---------------------------------------------------------------------------


def test_analyze_unauthenticated_returns_401(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = TestClient(app)

    response = client.post("/upload/analyze", headers=_csrf_headers(), json=_analyze_body())

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


def test_pat_header_cannot_authenticate_web_route(tmp_path) -> None:
    app = _make_app(tmp_path, logins=("patfriend",))
    token = issue_pat(app.state.pat_store, "patfriend", app.state.pat_hash_pepper_value)
    client = TestClient(app)

    # A valid PAT in the Authorization header must NOT authenticate the web
    # route: the helper is called with no header so only the session counts.
    response = client.post(
        "/upload/analyze",
        headers={"Authorization": f"Bearer {token}", **_csrf_headers()},
        json=_analyze_body(),
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Body-size guard (F01)
# ---------------------------------------------------------------------------


def test_oversized_body_rejected_before_processing(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)

    # A Content-Length above the cap is rejected before the body is parsed; the
    # body content is irrelevant because the guard never reads it.
    response = client.post(
        "/upload/analyze",
        headers={
            **_csrf_headers(),
            "Content-Length": str(MAX_ANALYZE_BODY_BYTES + 1),
            "Content-Type": "application/json",
        },
        content=b"{}",
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "UPLOAD_REJECTED"
    assert body["errors"] == [{"field": "file", "reason": "size"}]
    # Nothing was stored and the limiter was never charged (guard is pre-route).
    assert app.state.templates.list_rows() == []
    assert "friend" not in app.state.upload_limiter._windows


def test_body_guard_ignores_non_analyze_routes(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)
    analyzed = client.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]
    upload_id = analyzed["pending_upload_id"]

    # A large Content-Length on a non-analyze route is not guarded (the cap only
    # fronts the analyze paths). discard carries no body, so this just succeeds.
    response = client.post(
        f"/upload/discard/{upload_id}",
        headers={**_csrf_headers(), "Content-Length": str(MAX_ANALYZE_BODY_BYTES + 1)},
        content=b"",
    )
    assert response.status_code == 200


async def test_body_guard_rejects_chunked_body_without_content_length() -> None:
    # A POST to an analyze path with NO Content-Length (e.g. Transfer-Encoding:
    # chunked) must still be capped: the guard counts bytes as they arrive and
    # rejects on overflow rather than letting request.json() buffer unbounded.
    reached = False

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal reached
        reached = True

    guard = BodySizeGuardMiddleware(inner_app, max_bytes=MAX_ANALYZE_BODY_BYTES)
    scope = {"type": "http", "method": "POST", "path": "/upload/analyze", "headers": []}
    chunk = b"x" * (1024 * 1024)
    remaining = {"bytes": MAX_ANALYZE_BODY_BYTES + 2 * len(chunk)}

    async def receive() -> dict[str, Any]:
        if remaining["bytes"] > 0:
            remaining["bytes"] -= len(chunk)
            return {"type": "http.request", "body": chunk, "more_body": remaining["bytes"] > 0}
        return {"type": "http.request", "body": b"", "more_body": False}

    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await guard(scope, receive, send)

    assert reached is False  # inner app (and request.json) never reached
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 400
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert b"UPLOAD_REJECTED" in body


async def test_body_guard_replays_small_chunked_body_to_app() -> None:
    # A within-limit body with no Content-Length is buffered, then replayed intact to
    # the downstream app so the analyze route still parses it.
    received = bytearray()

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        more = True
        while more:
            message = await receive()
            received.extend(message.get("body", b""))
            more = message.get("more_body", False)

    guard = BodySizeGuardMiddleware(inner_app, max_bytes=MAX_ANALYZE_BODY_BYTES)
    scope = {"type": "http", "method": "POST", "path": "/upload/analyze", "headers": []}
    payload = b'{"ok": true}'
    chunks = [payload[:5], payload[5:]]

    async def receive() -> dict[str, Any]:
        if chunks:
            part = chunks.pop(0)
            return {"type": "http.request", "body": part, "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        pass

    await guard(scope, receive, send)

    assert bytes(received) == payload


# ---------------------------------------------------------------------------
# Discard removes only the owner row; the blob is left in place
# ---------------------------------------------------------------------------


def test_discard_removes_row_keeps_blob(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app)
    analyzed = client.post(
        "/upload/analyze", headers=_csrf_headers(), json=_analyze_body()
    ).json()["data"]
    upload_id = analyzed["pending_upload_id"]
    image_path = app.state.pending_uploads.get(upload_id, "friend").image_path

    response = client.post(f"/upload/discard/{upload_id}", headers=_csrf_headers())

    assert response.status_code == 200
    # The owner row is gone.
    import pytest

    with pytest.raises(KeyError):
        app.state.pending_uploads.get(upload_id, "friend")
    # The blob remains (reclamation is the grace-windowed gc-uploads sweep).
    assert app.state.image_store.get(image_path)


# ---------------------------------------------------------------------------
# U8: GET /upload page gating and content
# ---------------------------------------------------------------------------


def _csrf_from_page(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert match is not None, "CSRF meta tag missing from /upload page"
    return match.group(1)


def test_get_upload_unauthenticated_redirects_to_login(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = TestClient(app)

    response = client.get("/upload", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/upload"


def test_get_upload_authenticated_renders_page(tmp_path) -> None:
    app = _make_app(tmp_path)
    # Session without a CSRF token yet; GET /upload must mint one.
    client = _session_client(app, csrf=None)

    response = client.get("/upload")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    html = response.text
    # A CSRF token is minted and rendered into the meta tag.
    token = _csrf_from_page(html)
    assert token
    # Helper text covers formats, the 10 MB cap, the slow-VLM note, and the
    # EXIF/re-encode disclosure.
    assert "PNG" in html and "JPEG" in html and "WebP" in html
    assert "10 MB" in html
    assert "several seconds" in html
    assert "EXIF" in html and "re-encoded" in html
    # A <noscript> block states the flow requires JavaScript.
    assert "<noscript>" in html
    assert "JavaScript" in html


def test_get_upload_renders_nav_link(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app, csrf=None)

    response = client.get("/upload")

    assert 'href="/upload"' in response.text


# ---------------------------------------------------------------------------
# U8: browserless integration of the documented client contract
# ---------------------------------------------------------------------------


def test_browserless_analyze_then_approve_round_trip(tmp_path) -> None:
    app = _make_app(tmp_path)
    client = _session_client(app, csrf=None)

    # 1. Render the page and read the CSRF token exactly as the client would.
    page = client.get("/upload")
    token = _csrf_from_page(page.text)
    headers = _csrf_headers(token)

    # 2. POST the analyze JSON the client builds (base64 content + declared mime).
    analyzed = client.post("/upload/analyze", headers=headers, json=_analyze_body())
    assert analyzed.status_code == 200
    data = analyzed.json()["data"]
    pending_id = data["pending_upload_id"]
    assert pending_id

    # 3. POST the approve JSON with the (edited) metadata the client collects.
    approved = client.post(
        f"/upload/approve/{pending_id}",
        headers=headers,
        json={"metadata": data["metadata"], "ack_suspect": False},
    )
    assert approved.status_code == 200
    template_id = approved.json()["data"]["template_id"]

    # The round trip produced a friend-sourced template named from the metadata.
    template = app.state.templates.get(template_id)
    assert template.source == "friend"
    assert template.name == "Deploy Face"
