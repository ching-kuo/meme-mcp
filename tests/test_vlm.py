from meme_mcp.vlm.sanitize import flag_anomalies, hard_sanitize_metadata


def test_vlm_anomaly_flags_markup_and_zero_width() -> None:
    flags = flag_anomalies({"description": "<script>x</script>", "name": "zero\u200bwidth"})
    assert "markup" in flags
    assert "zero_width_unicode" in flags


def test_hard_sanitize_removes_markup_and_truncates() -> None:
    clean = hard_sanitize_metadata({"description": "<b>" + ("x" * 600) + "</b>"})
    assert "<" not in clean["description"]
    assert len(clean["description"]) == 512

