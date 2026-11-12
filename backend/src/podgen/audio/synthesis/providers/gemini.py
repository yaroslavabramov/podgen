from podgen.domain.errors import ContractError

from .common import audio_result, b64, json_response, options, prepared, voices


class Gemini:
    def prepare(self, profile, source):
        cast = voices(profile, source)
        if len(cast) > 2 or profile.output_format != "wav" or profile.sample_rate_hz != 24000:
            raise ContractError("unsupported_gemini_speech_format_or_cast")
        if len(cast) > 1 and any(v.startswith(("voice_", "voicekey_")) for v in cast.values()):
            raise ContractError("gemini_joint_custom_voices_unsupported")
        content = []
        for u in source.utterances:
            metadata = dict(type="speech_metadata")
            if len(cast) > 1:
                metadata["speaker"] = u.speaker
            if source.direction.style:
                metadata["style"] = source.direction.style
            content.append(dict(type="text", text=u.text, annotations=[metadata]))
        speech = (
            [dict(voice=next(iter(cast.values())))]
            if len(cast) == 1
            else dict(speakers=[dict(speaker=k, voice=v) for k, v in cast.items()])
        )
        body = dict(
            model=profile.model_id,
            input=[dict(type="user_input", content=content)],
            response_format=dict(type="audio"),
            generation_config=dict(speech_config=speech, **options(profile, ("seed", "max_output_tokens"))),
        )
        return prepared(
            profile, source, "https://generativelanguage.googleapis.com/v1beta/interactions", body
        )

    def decode(self, request, receipt):
        value = json_response(request, receipt)
        if value.get("status") != "completed":
            raise ContractError("gemini_synthesis_not_completed")
        outputs = [
            part
            for step in value.get("steps", [])
            if step.get("type") == "model_output"
            for part in step.get("content", [])
            if part.get("type") == "audio"
        ]
        if len(outputs) != 1 or outputs[0].get("mime_type") != "audio/wav":
            raise ContractError("gemini_expected_one_complete_wav")
        u = value.get("usage", {})
        usage = {}
        if type(u.get("total_input_tokens")) is int:
            usage["input_tokens"] = u["total_input_tokens"]
        audio = [r["tokens"] for r in u.get("output_tokens_by_modality", []) if r.get("modality") == "audio"]
        if audio:
            usage["audio_tokens"] = sum(audio)
        return audio_result(
            request,
            receipt,
            b64(outputs[0]["data"]),
            usage=usage,
            basis="provider_reported",
            request_id=value.get("id"),
        )
