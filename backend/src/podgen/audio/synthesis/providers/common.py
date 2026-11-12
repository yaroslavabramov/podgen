import base64
import json

from podgen.domain.errors import ContractError

from ..contracts import PreparedRequest, SynthesizedAudio


def voices(profile, source):
    speakers = list(dict.fromkeys(u.speaker for u in source.utterances))
    if source.output_language != profile.output_language:
        raise ContractError("synthesis_language_profile_mismatch")
    if source.speaker_voices:
        if set(source.speaker_voices) != set(speakers) or not all(source.speaker_voices.values()):
            raise ContractError("incomplete_synthesis_voice_binding")
        return source.speaker_voices
    if len(speakers) != 1:
        raise ContractError("explicit_dialogue_voices_required")
    return {speakers[0]: profile.voice_id}


def options(profile, allowed):
    if set(profile.options) - set(allowed):
        raise ContractError("unsupported_synthesis_option")
    from ..options import validate

    validate(profile.options)
    return dict(profile.options)


def prepared(profile, source, url, body, *, query=None, headers=None):
    if "text" in body:
        text = body["text"]
    elif "inputs" in body:
        text = "".join(row["text"] for row in body["inputs"])
    elif "script" in body:
        text = "".join(row["text"] for row in body["script"]["utterances"])
    else:
        text = "".join(row["text"] for row in body["input"][0]["content"])
    return PreparedRequest(
        adapter_version=f"{profile.provider}-synthesis/1",
        provider=profile.provider,
        model_id=profile.model_id,
        url=url,
        body=body,
        query=query or {},
        headers={"content-type": "application/json", **(headers or {})},
        text_characters=len(text),
        text_utf8_bytes=len(text.encode()),
        output_format=profile.output_format,
        sample_rate_hz=profile.sample_rate_hz,
    )


def json_response(request, receipt):
    if not receipt.complete or not 200 <= receipt.status_code < 300:
        raise ContractError("synthesis_response_not_complete")
    value = json.loads(receipt.body)
    model = value.get("model") or value.get("model_id") or value.get("usage", {}).get("modelId")
    if model is not None and model != request.model_id:
        raise ContractError("synthesis_provider_model_mismatch")
    return value


def audio_result(
    request, receipt, audio, *, usage=None, basis="submitted_text_estimate", alignment=None, request_id=None
):
    if not audio:
        raise ContractError("empty_synthesis_audio")
    if usage is not None and any(type(v) is not int or v < 0 for v in usage.values()):
        raise ContractError("invalid_synthesis_usage")
    media = {"wav": "audio/wav", "flac": "audio/flac", "pcm_s16le": "audio/pcm"}[request.output_format]
    return SynthesizedAudio(
        audio,
        media,
        request.sample_rate_hz,
        request.output_format,
        request_id or receipt.headers.get("request-id") or receipt.headers.get("x-request-id"),
        usage if usage is not None else {"characters": request.text_characters},
        basis,
        alignment,
    )


def b64(value):
    return base64.b64decode(value, validate=True)
