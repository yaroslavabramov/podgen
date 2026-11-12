import base64
import json

import pytest

from podgen.audio.fidelity.adjudication.openai import format_instruction, parse, prepare
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.adjudication.contracts import OpenAIReviewerProfile
from tests.audio_cases import signal
from tests.test_audio_adjudication import decision


def response(content=None, **choice):
    return json.dumps(
        dict(
            id="openai-fixture",
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(content=content or json.dumps(dict(results=[decision()]))),
                    **choice,
                )
            ],
            usage=dict(prompt_tokens=100, completion_tokens=50),
        )
    ).encode()


def test_openai_frozen_wire_preserves_audio_payload_prompt_and_schema():
    prompt = "Approved neutral prompt.\n"
    payload = dict(output_language="ru", cases=[dict(case_id="C71")])
    request, wire = prepare(prompt, payload, signal(), OpenAIReviewerProfile())
    body = json.loads(wire)
    assert body["model"] == "gpt-audio-1.5" and body["modalities"] == ["text"]
    assert body["messages"][0] == dict(role="system", content=prompt)
    assert body["messages"][1]["content"] == format_instruction()
    content = body["messages"][2]["content"]
    assert json.loads(content[0]["text"]) == payload
    assert base64.b64decode(content[1]["input_audio"]["data"]) == signal()
    assert "response_format" not in body and "tools" not in body
    assert request["url"] == "https://api.openai.com/v1/chat/completions"
    parsed, usage, request_id = parse(response(), ["C71"], 5)
    assert parsed.results[0].status == "dismissed" and usage["prompt_tokens"] == 100
    assert request_id == "openai-fixture"


@pytest.mark.parametrize("content", ["```json\n{}\n```", '{"results":[]}', "not JSON"])
def test_openai_rejects_invalid_contract(content):
    with pytest.raises(ValueError):
        parse(response(content), ["C71"], 5)


def test_openai_rejects_truncated_response():
    raw = json.loads(response())
    raw["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ContractError):
        parse(json.dumps(raw).encode(), ["C71"], 5)


@pytest.mark.parametrize("choices", [[None], [dict(finish_reason="stop", message=None)], []])
def test_openai_rejects_malformed_envelope(choices):
    with pytest.raises(ContractError):
        parse(json.dumps(dict(choices=choices)).encode(), ["C71"], 5)
