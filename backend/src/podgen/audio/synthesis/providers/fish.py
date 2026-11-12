from podgen.domain.errors import ContractError

from .common import audio_result, options, prepared, voices


class Fish:
    def prepare(self, profile, source):
        cast = voices(profile, source)
        if profile.output_format not in ("wav", "pcm_s16le") or profile.sample_rate_hz not in (
            8000,
            16000,
            24000,
            32000,
            44100,
        ):
            raise ContractError("unsupported_fish_lossless_format")
        if source.direction.style:
            raise ContractError("fish_direction_requires_explicit_inline_tag_plan")
        speaker_ids = list(cast)
        text = "\n".join(
            (f"<|speaker:{speaker_ids.index(u.speaker)}|>" if len(cast) > 1 else "") + u.text
            for u in source.utterances
        )
        body = dict(
            text=text,
            language=source.output_language,
            reference_id=next(iter(cast.values())) if len(cast) == 1 else list(cast.values()),
            format="pcm" if profile.output_format == "pcm_s16le" else "wav",
            sample_rate=profile.sample_rate_hz,
            **options(profile, ("temperature", "top_p", "chunk_length", "normalize", "latency")),
        )
        return prepared(profile, source, "https://api.fish.audio/v1/tts", body, headers=dict(model="drama-3"))

    def decode(self, request, receipt):
        if (
            request.headers.get("model") != "drama-3"
            or not receipt.complete
            or not 200 <= receipt.status_code < 300
        ):
            raise ContractError("fish_exact_model_or_response_invalid")
        model = receipt.headers.get("model") or receipt.headers.get("x-model")
        if model is not None and model != "drama-3":
            raise ContractError("synthesis_provider_model_mismatch")
        return audio_result(
            request,
            receipt,
            receipt.body,
            usage={"utf8_bytes": len(request.body["text"].encode())},
            basis="submitted_text_estimate",
        )
