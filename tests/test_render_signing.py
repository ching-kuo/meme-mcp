from meme_mcp.rendering.signing import sign_render_url, verify_render_signature

SECRET = "x" * 32
PATH = "24/26e4cd978d0019.png"
NOW = 1_000_000


def _sig_from(url: str) -> str:
    return url.split("sig=", 1)[1]


def _exp_from(url: str) -> str:
    return url.split("exp=", 1)[1].split("&", 1)[0]


def test_sign_render_url_appends_exp_and_sig() -> None:
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    assert url.startswith("https://host/renders/" + PATH + "?")
    assert f"exp={NOW + 3600}" in url
    assert "sig=" in url


def test_valid_signature_verifies() -> None:
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    assert verify_render_signature(PATH, _exp_from(url), _sig_from(url), SECRET, NOW) is True


def test_expired_signature_rejected() -> None:
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    later = NOW + 3601
    assert verify_render_signature(PATH, _exp_from(url), _sig_from(url), SECRET, later) is False


def test_tampered_signature_rejected() -> None:
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    assert verify_render_signature(PATH, _exp_from(url), "x" + _sig_from(url), SECRET, NOW) is False


def test_wrong_secret_rejected() -> None:
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    assert verify_render_signature(PATH, _exp_from(url), _sig_from(url), "y" * 32, NOW) is False


def test_different_path_rejected() -> None:
    """A signature for one blob does not authorize another (no path confusion)."""
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    other = "ab/deadbeef.png"
    assert verify_render_signature(other, _exp_from(url), _sig_from(url), SECRET, NOW) is False


def test_non_integer_exp_rejected() -> None:
    url = sign_render_url("https://host/renders/" + PATH, SECRET, NOW, 3600)
    assert verify_render_signature(PATH, "notanint", _sig_from(url), SECRET, NOW) is False
