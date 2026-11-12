import base64
import json

from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError

from .contracts import WindowResolution

VERSION = "gemini-actual-audio-fidelity-review/2"


def prepare(prompt, payload, audio, profile):
    body = dict(
        model=profile.model_id,
        system_instruction=prompt,
        store=False,
        input=[
            dict(type="text", text=json.dumps(payload, ensure_ascii=False)),
            dict(type="audio", data=base64.b64encode(audio).decode(), mime_type="audio/wav"),
        ],
        response_format=dict(
            type="text", mime_type="application/json", schema=WindowResolution.model_json_schema()
        ),
        generation_config=dict(
            max_output_tokens=profile.max_output_tokens, thinking_level=profile.thinking_level
        ),
    )
    wire = canonical_bytes(body)
    if len(wire) >= 20_000_000:
        raise ContractError("review_inline_request_exceeds_20mb")
    return dict(
        adapter_version=VERSION,
        method="POST",
        url="https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={
            "content-type": "application/json",
            "accept-encoding": "identity",
            "Api-Revision": "2026-05-20",
        },
    ), wire


def parse(raw, case_ids, duration):
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("status") != "completed":
        raise ContractError("review_model_response_not_completed")
    blocks = [
        part["text"]
        for step in value.get("steps", [])
        if step.get("type") == "model_output"
        for part in step.get("content", [])
        if part.get("type") == "text"
    ]
    if not blocks:
        raise ContractError("review_model_response_missing_text")
    resolved = WindowResolution.model_validate_json("".join(blocks)).validate_window(case_ids, duration)
    usage = value.get("usage", {})
    if not isinstance(usage, dict):
        raise ContractError("review_invalid_usage_object")
    return resolved, usage, value.get("id")
