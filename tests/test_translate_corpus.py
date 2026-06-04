from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from meme_mcp.cli.translate_corpus import generate_overlay, run


class _FakeChatCompletions:
    def __init__(self, fake: _FakeLLM) -> None:
        self._fake = fake

    def create(self, **kwargs: Any) -> Any:
        self._fake.calls += 1
        messages = kwargs["messages"]
        system = messages[0]["content"]
        retry = "previous attempt" in system
        # The user message embeds the source JSON; key the canned response on
        # whichever slug's description appears in it.
        user = messages[1]["content"]
        payload = self._fake.responder(user, retry)
        tool_call = SimpleNamespace(
            function=SimpleNamespace(arguments=json.dumps(payload, ensure_ascii=False))
        )
        message = SimpleNamespace(tool_calls=[tool_call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeLLM:
    """OpenAI-compatible fake: `.chat.completions.create(...)` returns a tool call."""

    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.calls = 0
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(self))


def _settings() -> Any:
    return SimpleNamespace(
        vlm_model="fake-model",
        vlm_api_key=SimpleNamespace(get_secret_value=lambda: "k"),
        vlm_base_url="http://fake",
    )


def _write_enrichment(path: Path, entries: dict[str, Any]) -> None:
    data = {"_meta": {"memegen_commit": "abc123", "model": "sonnet"}, **entries}
    path.write_text(json.dumps(data), encoding="utf-8")


_CLEAN_RESPONSE = {
    "description": "用於陳述顯而易見的事實。",
    "emotion": "得意",
    "usage_context": "面對顯而易見的事實時的反應",
    "tags": ["顯而易見", "迷因"],
}


def test_generate_overlay_writes_translations_and_provenance_meta() -> None:
    llm = _FakeLLM(lambda user, retry: dict(_CLEAN_RESPONSE))
    enrichment = {
        "tenguy": {
            "description": "Used when stating something obviously true.",
            "emotion": "smug",
            "usage_context": "reacting to an obvious fact",
            "extra_tags": ["obvious"],
        }
    }
    overlay = generate_overlay(llm, "fake-model", enrichment, memegen_commit="abc123")

    assert overlay["tenguy"] == _CLEAN_RESPONSE
    assert overlay["_meta"]["model"] == "fake-model"
    assert overlay["_meta"]["memegen_commit"] == "abc123"
    assert "name" not in overlay["tenguy"]
    assert llm.calls == 1


def test_run_writes_artifact_and_prints_sentinel(tmp_path: Path, capsys: Any) -> None:
    source = tmp_path / "memegen-enrichment.json"
    target = tmp_path / "out.zh-TW.json"
    _write_enrichment(source, {"tenguy": {"description": "x", "extra_tags": []}})
    llm = _FakeLLM(lambda user, retry: dict(_CLEAN_RESPONSE))

    rc = run(_settings(), enrichment_path=source, output_path=target, llm=llm)

    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["tenguy"]["description"] == _CLEAN_RESPONSE["description"]
    out = capsys.readouterr().out
    assert "generated 1/1 slugs" in out
    assert "reindex-embeddings --force" in out


def test_drift_failing_value_retries_then_skips_field() -> None:
    drifted = {
        "description": "這個視頻很好",  # mainland vocab -> drift fail, both attempts
        "emotion": "得意",
        "usage_context": "情境",
        "tags": ["標籤"],
    }
    llm = _FakeLLM(lambda user, retry: dict(drifted))
    enrichment = {"v": {"description": "a video", "extra_tags": []}}

    overlay = generate_overlay(llm, "fake-model", enrichment)

    # The drifted description is retried once then skipped -- never written.
    assert "視頻" not in json.dumps(overlay, ensure_ascii=False)
    assert "description" not in overlay["v"]
    # Clean fields from the first attempt are kept.
    assert overlay["v"]["emotion"] == "得意"
    assert overlay["v"]["tags"] == ["標籤"]
    assert llm.calls == 2  # one initial + one retry


def test_drift_retry_succeeds_recovers_field() -> None:
    def responder(user: str, retry: bool) -> dict[str, Any]:
        if retry:
            return dict(_CLEAN_RESPONSE)
        return {
            "description": "這個視頻很好",  # drifts on first attempt
            "emotion": "得意",
            "usage_context": "情境",
            "tags": ["標籤"],
        }

    llm = _FakeLLM(responder)
    enrichment = {"v": {"description": "a video", "extra_tags": []}}

    overlay = generate_overlay(llm, "fake-model", enrichment)

    assert overlay["v"]["description"] == _CLEAN_RESPONSE["description"]
    assert llm.calls == 2


def test_regenerate_is_idempotent_and_skips_existing_slugs() -> None:
    llm = _FakeLLM(lambda user, retry: dict(_CLEAN_RESPONSE))
    enrichment = {"tenguy": {"description": "x", "extra_tags": []}}
    existing = {"_meta": {"model": "old"}, "tenguy": dict(_CLEAN_RESPONSE)}

    overlay = generate_overlay(llm, "fake-model", enrichment, existing=existing)

    assert overlay["tenguy"] == _CLEAN_RESPONSE
    # An already-translated slug is reused, not re-sent to the LLM.
    assert llm.calls == 0
