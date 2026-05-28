from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from meme_mcp.app import create_app
from meme_mcp.auth.pat import issue_pat
from meme_mcp.db.templates import TemplateCreate
from tests.test_upload_flow import good_settings, png_bytes


def _seed_app_and_token(tmp_path, *, ttl_days: int | None) -> tuple[TestClient, str]:
    app = create_app(good_settings(tmp_path))
    token = issue_pat(
        app.state.pat_store,
        "friend",
        app.state.pat_hash_pepper_value,
        ttl_days=ttl_days,
    )
    app.state.allowlist.add("friend")
    image_path = app.state.image_store.put(png_bytes(), "png")
    app.state.templates.upsert(
        TemplateCreate(
            template_id="t1",
            slug="t1",
            name="Template 1",
            source="friend",
            metadata={
                "name": "Template 1",
                "description": "d",
                "emotion": "e",
                "usage_context": "u",
                "tags": [],
                "format": "static",
            },
            slot_definitions=[{"name": "top", "position": "top"}],
            image_path=image_path,
            perceptual_hash="0" * 16,
            exact_hash="a" * 64,
        )
    )
    return TestClient(app), token


def _set_expires_at(pat_store_path, login: str, expires_at: datetime | None) -> None:
    with sqlite3.connect(pat_store_path) as conn:
        conn.execute(
            "UPDATE pats SET expires_at = ? WHERE friend_login = ? AND revoked_at IS NULL",
            (expires_at.isoformat() if expires_at else None, login),
        )


def test_browse_renders_no_banner_when_pat_has_long_expiry(tmp_path) -> None:
    client, token = _seed_app_and_token(tmp_path, ttl_days=90)
    response = client.get("/browse", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "PAT expires in" not in response.text


def test_browse_renders_no_banner_when_pat_has_no_expiry(tmp_path) -> None:
    client, token = _seed_app_and_token(tmp_path, ttl_days=0)
    response = client.get("/browse", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "PAT expires in" not in response.text


def test_browse_renders_banner_when_pat_expires_within_seven_days(tmp_path) -> None:
    client, token = _seed_app_and_token(tmp_path, ttl_days=90)
    _set_expires_at(
        client.app.state.pat_store.path,
        "friend",
        datetime.now(UTC) + timedelta(days=3, hours=12),
    )
    response = client.get("/browse", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "PAT expires in 3 days" in response.text
    assert "friend" in response.text


def test_browse_renders_banner_with_singular_day(tmp_path) -> None:
    client, token = _seed_app_and_token(tmp_path, ttl_days=90)
    _set_expires_at(
        client.app.state.pat_store.path,
        "friend",
        datetime.now(UTC) + timedelta(days=1, hours=12),
    )
    response = client.get("/browse", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "PAT expires in 1 day." in response.text


def test_browse_renders_banner_with_zero_days_for_same_day_expiry(tmp_path) -> None:
    client, token = _seed_app_and_token(tmp_path, ttl_days=90)
    _set_expires_at(
        client.app.state.pat_store.path,
        "friend",
        datetime.now(UTC) + timedelta(hours=3),
    )
    response = client.get("/browse", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "PAT expires in 0 days" in response.text
