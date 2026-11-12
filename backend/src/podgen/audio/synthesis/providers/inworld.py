from podgen.domain.errors import ContractError

from .common import audio_result, b64, json_response, options, prepared, voices


class Inworld:
    def prepare(self, profile, source):
        cast = voices(profile, source)
        text_units = sum(len(u.text.encode("utf-16-le")) // 2 for u in source.utterances)
        if not 1 <= len(source.utterances) <= 100 or text_units > 2000:
            raise ContractError("inworld_script_utf16_limit")
        if source.direction.style:
            raise ContractError("inworld_direction_requires_explicit_inline_tag_plan")
        if not 8000 <= profile.sample_rate_hz <= 48000:
            raise ContractError("unsupported_inworld_sample_rate")
        encoding = {"wav": "LINEAR16", "pcm_s16le": "PCM", "flac": "FLAC"}[profile.output_format]
        opts = options(profile, ("deliveryMode", "speakingRate", "timestampType"))
        body = dict(
            modelId=profile.model_id,
            language="ru-RU",
            outputMode="COMBINED",
            audioConfig=dict(audioEncoding=encoding, sampleRateHertz=profile.sample_rate_hz),
            script=dict(
                utterances=[
                    dict(text=u.text, voiceConfig=dict(voiceId=cast[u.speaker])) for u in source.utterances
                ]
            ),
        )
        if "speakingRate" in opts:
            body["audioConfig"]["speakingRate"] = opts.pop("speakingRate")
        body.update(opts)
        return prepared(profile, source, "https://api.inworld.ai/tts/v1/voice", body)

    def decode(self, request, receipt):
        value = json_response(request, receipt)
        u = value.get("usage", {})
        usage = (
            {"characters": u["processedCharactersCount"]}
            if type(u.get("processedCharactersCount")) is int
            else {}
        )
        return audio_result(
            request,
            receipt,
            b64(value["audioContent"]),
            usage=usage,
            basis="provider_reported_utf16_units",
            alignment=value.get("timestampInfo"),
        )
