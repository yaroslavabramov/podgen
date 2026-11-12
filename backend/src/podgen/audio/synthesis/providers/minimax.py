from podgen.domain.errors import ContractError

from .common import audio_result, json_response, options, prepared, voices


class MiniMax:
    def prepare(self, profile, source):
        cast = voices(profile, source)
        if len(cast) != 1:
            raise ContractError("minimax_joint_dialogue_not_supported")
        text = "\n".join(u.text for u in source.utterances)
        if len(text) >= 10000:
            raise ContractError("minimax_text_limit")
        if source.direction.style:
            raise ContractError("minimax_freeform_style_not_supported")
        if profile.sample_rate_hz not in (8000, 16000, 22050, 24000, 32000, 44100):
            raise ContractError("unsupported_minimax_sample_rate")
        opts = options(profile, ("speed", "vol", "pitch", "emotion", "pronunciation_dict"))
        pronunciation = opts.pop("pronunciation_dict", None)
        body = dict(
            model=profile.model_id,
            text=text,
            stream=False,
            output_format="hex",
            language_boost="Russian",
            voice_setting=dict(voice_id=next(iter(cast.values())), **opts),
            audio_setting=dict(
                sample_rate=profile.sample_rate_hz,
                format="pcm" if profile.output_format == "pcm_s16le" else profile.output_format,
                channel=1,
            ),
        )
        if pronunciation is not None:
            body["pronunciation_dict"] = pronunciation
        return prepared(profile, source, "https://api.minimax.io/v1/t2a_v2", body)

    def decode(self, request, receipt):
        value = json_response(request, receipt)
        if value.get("base_resp", {}).get("status_code") != 0 or (value.get("data") or {}).get("status") != 2:
            raise ContractError("minimax_synthesis_not_completed")
        info = value.get("extra_info", {})
        expected_format = "pcm" if request.output_format == "pcm_s16le" else request.output_format
        for key, expected in (
            ("audio_sample_rate", request.sample_rate_hz),
            ("audio_format", expected_format),
            ("audio_channel", 1),
        ):
            if key in info and info[key] != expected:
                raise ContractError("minimax_reported_audio_format_mismatch")
        usage = {"characters": info["usage_characters"]} if type(info.get("usage_characters")) is int else {}
        return audio_result(
            request,
            receipt,
            bytes.fromhex(value["data"]["audio"]),
            usage=usage,
            basis="provider_reported",
            request_id=value.get("trace_id"),
        )
