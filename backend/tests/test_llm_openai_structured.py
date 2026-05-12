from types import SimpleNamespace

import pytest

from app.services import llm


@pytest.mark.asyncio
async def test_call_openai_structured_uses_responses_json_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class _FakeResponses:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text='{"ok":true}',
                status="completed",
                incomplete_details=None,
                usage=SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14),
            )

    class _FakeClient:
        responses = _FakeResponses()

    monkeypatch.setattr(llm, "get_openai_client", lambda: _FakeClient())

    raw = await llm.call_openai_structured(
        system="system",
        prompt="prompt",
        model="gpt-5.4",
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        reasoning_effort="medium",
    )

    assert raw == '{"ok":true}'
    assert captured["model"] == "gpt-5.4"
    assert captured["instructions"].endswith("You MUST respond with valid JSON only. No markdown, no explanation.")
    assert captured["input"] == "prompt"
    assert captured["temperature"] is llm.openai.NOT_GIVEN
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["text"] == {
        "format": {
            "type": "json_schema",
            "name": "structured_response",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "strict": True,
        }
    }
