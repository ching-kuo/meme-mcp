from fastapi.testclient import TestClient

from meme_mcp.app import create_app


def test_health_and_envelope_error_shape() -> None:
    client = TestClient(create_app())
    assert client.get("/healthz").json() == {"ok": True}
    response = client.get("/api/missing")
    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"

