import json

import httpx
from openai import APIConnectionError, BadRequestError

from meme_mcp.vlm.client import VLMClient
from meme_mcp.vlm.sanitize import flag_anomalies, hard_sanitize_metadata


def test_vlm_anomaly_flags_markup_and_zero_width() -> None:
    flags = flag_anomalies({"description": "<script>x</script>", "name": "zero\u200bwidth"})
    assert "markup" in flags
    assert "zero_width_unicode" in flags


def test_hard_sanitize_removes_markup_and_truncates() -> None:
    clean = hard_sanitize_metadata({"description": "<b>" + ("x" * 600) + "</b>"})
    assert "<" not in clean["description"]
    assert len(clean["description"]) == 512


class FakeCompletions:
    def create(self, **kwargs):
        self.kwargs = kwargs

        class Function:
            arguments = json.dumps(
                {
                    "name": "CI Party",
                    "description": "celebrate clean CI",
                    "emotion": "joy",
                    "usage_context": "after tests pass",
                    "tags": ["ci"],
                    "format": "static",
                    "slot_definitions": [{"name": "top", "position": "top"}],
                }
            )

        class ToolCall:
            function = Function()

        class Message:
            tool_calls = [ToolCall()]

        class Choice:
            message = Message()

        class Response:
            choices = [Choice()]

        return Response()


class FakeChat:
    def __init__(self) -> None:
        self.completions = FakeCompletions()


class FakeVLMProvider:
    def __init__(self) -> None:
        self.chat = FakeChat()


def test_vlm_client_parses_forced_tool_call() -> None:
    fake = FakeVLMProvider()
    result = VLMClient(model="vlm-model", provider=fake).enrich_template(b"png-bytes", "hint")
    assert result.status == "success"
    assert result.metadata is not None
    assert result.metadata["name"] == "CI Party"
    assert fake.chat.completions.kwargs["tool_choice"] == "required"
    tools = fake.chat.completions.kwargs["tools"]
    assert tools[0]["function"]["name"] == "record_template_metadata"


class _RaisingCompletions:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def create(self, **_kwargs):
        raise self._exc


class _RaisingProvider:
    def __init__(self, exc: Exception) -> None:
        self.chat = type("Chat", (), {"completions": _RaisingCompletions(exc)})()


def test_vlm_client_returns_status_flag_on_http_error() -> None:
    response = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    exc = BadRequestError(message="bad", response=response, body=None)
    result = VLMClient(model="m", provider=_RaisingProvider(exc)).enrich_template(b"x")
    assert result.status == "error"
    assert result.suspect_flags == ["vlm_400"]


def test_vlm_client_returns_network_flag_on_connection_error() -> None:
    exc = APIConnectionError(request=httpx.Request("POST", "http://x"))
    result = VLMClient(model="m", provider=_RaisingProvider(exc)).enrich_template(b"x")
    assert result.status == "error"
    assert result.suspect_flags == ["vlm_network"]
