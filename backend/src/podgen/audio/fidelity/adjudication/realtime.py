import base64
import json

from podgen.audio.media.review_projection import pcm, project
from podgen.domain.canonical import canonical_bytes
from podgen.domain.errors import ContractError

from .contracts import WindowResolution

VERSION = "openai-realtime-actual-audio-review/2"
URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
TEXT_ITEM_ID = "item_review_payload"
TOOL = "submit_audio_review"


def prepare_projected(prompt, payload, audio, profile, proof):
    data, width, rate, _ = pcm(audio)
    if (width, rate) != (2, 24000):
        raise ContractError("realtime_requires_pcm16_mono24k")
    events = [
        dict(
            type="session.update",
            session=dict(
                type="realtime",
                model=profile.model_id,
                output_modalities=["text"],
                instructions=prompt + "\n\nReturn the results by calling submit_audio_review.",
                reasoning=dict(effort=profile.reasoning_effort),
                max_output_tokens=profile.max_output_tokens,
                tools=[
                    dict(
                        type="function",
                        name=TOOL,
                        description="Return the results of checking the supplied audio window.",
                        parameters=WindowResolution.model_json_schema(),
                    )
                ],
                tool_choice=dict(type="function", name=TOOL),
                audio=dict(input=dict(format=dict(type="audio/pcm", rate=24000), turn_detection=None)),
            ),
        ),
        dict(
            type="conversation.item.create",
            item=dict(
                id=TEXT_ITEM_ID,
                type="message",
                role="user",
                content=[dict(type="input_text", text=json.dumps(payload, ensure_ascii=False))],
            ),
        ),
        dict(type="input_audio_buffer.append", audio=base64.b64encode(data).decode()),
        dict(type="input_audio_buffer.commit"),
        dict(
            type="response.create",
            response=dict(output_modalities=["text"], max_output_tokens=profile.max_output_tokens),
        ),
    ]
    return dict(
        adapter_version=VERSION, transport="websocket", url=URL, audio_projection=proof
    ), canonical_bytes(events)


def prepare(prompt, payload, audio, profile):
    projected, proof = project(audio)
    return prepare_projected(prompt, payload, projected, profile, proof)


def parse(raw, case_ids, duration):
    transcript = json.loads(raw)
    if not isinstance(transcript, dict) or not isinstance(transcript.get("events"), list):
        raise ContractError("review_realtime_invalid_transcript")
    if any(
        not isinstance(e, dict)
        or not isinstance(e.get("raw"), str)
        or e.get("direction") not in ("server", "client", "handshake")
        for e in transcript["events"]
    ):
        raise ContractError("review_realtime_invalid_transcript")
    events = [json.loads(e["raw"]) for e in transcript["events"] if e["direction"] == "server"]
    if any(not isinstance(e, dict) for e in events):
        raise ContractError("review_realtime_invalid_transcript")
    done = [e for e in events if e.get("type") == "response.done"]
    if len(done) != 1 or any(e.get("type") == "error" for e in events):
        raise ContractError("review_realtime_missing_unique_done")
    response = done[0]["response"]
    if not isinstance(response, dict) or response.get("status") != "completed":
        raise ContractError("review_model_response_not_completed")
    output = response.get("output")
    if not isinstance(output, list) or any(
        not isinstance(item, dict) or item.get("type") not in ("message", "function_call") for item in output
    ):
        raise ContractError("review_realtime_requires_one_function_call")
    calls = [item for item in output if item.get("type") == "function_call"]
    if len(calls) != 1:
        raise ContractError("review_realtime_requires_one_function_call")
    call = calls[0]
    if (
        not isinstance(call, dict)
        or call.get("type") != "function_call"
        or call.get("name") != TOOL
        or call.get("status") != "completed"
    ):
        raise ContractError("review_realtime_requires_one_function_call")
    parsed = WindowResolution.model_validate_json(call["arguments"]).validate_window(case_ids, duration)
    usage = response.get("usage", {})
    if not isinstance(usage, dict):
        raise ContractError("review_invalid_usage_object")
    return parsed, usage, response.get("id")
