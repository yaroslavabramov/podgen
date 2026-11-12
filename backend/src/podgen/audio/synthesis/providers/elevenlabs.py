from podgen.domain.errors import ContractError

from .common import audio_result, b64, json_response, options, prepared, voices


class ElevenLabs:
    def prepare(self, profile, source):
        cast = voices(profile, source)
        if profile.output_format not in ("wav", "pcm_s16le") or profile.sample_rate_hz not in (
            16000,
            22050,
            24000,
            44100,
        ):
            raise ContractError("unsupported_eleven_lossless_format")
        if source.direction.style:
            raise ContractError("eleven_direction_requires_explicit_inline_tag_plan")
        if sum(len(u.text) for u in source.utterances) > 2000:
            raise ContractError("eleven_dialogue_reliable_text_limit")
        body = dict(
            inputs=[dict(text=u.text, voice_id=cast[u.speaker]) for u in source.utterances],
            model_id=profile.model_id,
            language_code=source.output_language,
            **options(profile, ("seed", "apply_text_normalization")),
        )
        encoding = "wav" if profile.output_format == "wav" else "pcm"
        return prepared(
            profile,
            source,
            "https://api.elevenlabs.io/v1/text-to-dialogue/with-timestamps",
            body,
            query=dict(output_format=f"{encoding}_{profile.sample_rate_hz}"),
        )

    def decode(self, request, receipt):
        value = json_response(request, receipt)
        return audio_result(
            request,
            receipt,
            b64(value["audio_base64"]),
            alignment={
                k: value[k] for k in ("alignment", "normalized_alignment", "voice_segments") if k in value
            },
        )
