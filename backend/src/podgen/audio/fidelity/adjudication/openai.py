"""Actual-audio Chat Completions adapter; JSON validation remains local."""

import base64
import json

from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError

from .contracts import WindowResolution

VERSION = "openai-actual-audio-fidelity-review/2"


def format_instruction():
    return (
        "Return one JSON object matching the following JSON Schema exactly. "
        "Do not use Markdown fences or include any text outside the JSON object.\n"
        + json.dumps(WindowResolution.model_json_schema(), ensure_ascii=False, sort_keys=True)
    )


def prepare(prompt, payload, audio, profile):
    body = dict(
        model=profile.model_id,
        modalities=["text"],
        store=False,
        max_completion_tokens=profile.max_output_tokens,
        messages=[
            dict(role="system", content=prompt),
            dict(role="system", content=format_instruction()),
            dict(
                role="user",
                content=[
                    dict(type="text", text=json.dumps(payload, ensure_ascii=False)),
                    dict(
                        type="input_audio",
                        input_audio=dict(data=base64.b64encode(audio).decode(), format="wav"),
                    ),
                ],
            ),
        ],
    )
    wire = canonical_bytes(body)
    if len(wire) >= 20_000_000:
        raise ContractError("review_inline_request_exceeds_20mb")
    return dict(
        adapter_version=VERSION,
        method="POST",
        url="https://api.openai.com/v1/chat/completions",
        headers={"content-type": "application/json", "accept-encoding": "identity"},
    ), wire


def parse(raw, case_ids, duration):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ContractError("review_model_response_not_completed")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ContractError("review_model_response_not_completed")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
        raise ContractError("review_model_response_not_completed")
    message = choice.get("message", {})
    if not isinstance(message, dict):
        raise ContractError("review_model_response_missing_text")
    content = message.get("content")
    if message.get("refusal") or not isinstance(content, str) or not content.strip():
        raise ContractError("review_model_response_missing_text")
    resolved = WindowResolution.model_validate_json(content).validate_window(case_ids, duration)
    usage = value.get("usage", {})
    if not isinstance(usage, dict):
        raise ContractError("review_invalid_usage_object")
    return resolved, usage, value.get("id")
