"""Decode and process an already archived response; this module never calls a provider."""

from podgen.audio.media.processing import VERSION, decode, normalize, tools
from podgen.audio.synthesis.registry import adapter
from podgen.domain.errors import ContractError

from .costs import estimate


def process(journal, key, profile, config, request, response, *, output_key=None, reason=None):
    output_key = output_key or key
    attempt = journal.get(key + ":prepared")
    if journal.get(output_key + ":processing") is None:
        journal.put(
            output_key + ":processing",
            dict(
                contract="audio-processing-attempt/1",
                processing_contract=VERSION,
                tool_versions=tools(),
                source_receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
                reason=reason,
            ),
        )
    phase = "provider_decode"
    try:
        audio = adapter(profile.synthesis.provider).decode(request, response)
        audio_blob = journal.blob(audio.audio, audio.media_type)
        journal.put(
            key + ":audio",
            dict(
                contract="audio-provider-output/1",
                provider_audio_blob=audio_blob,
                encoding=audio.encoding,
                sample_rate_hz=audio.sample_rate_hz,
                request_id=audio.request_id,
                usage=audio.usage,
                usage_basis=audio.usage_basis,
                alignment=audio.alignment,
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            ),
        )
        phase = "media_processing"
        pcm, metrics = decode(audio)
        pcm_blob = journal.blob(pcm, "audio/wav")
        journal.put(
            output_key + ":decoded",
            dict(
                contract="audio-decoded-output/1",
                pcm_blob=pcm_blob,
                metrics=metrics,
                provider_output_ref=journal.ref(key + ":audio").model_dump(mode="json"),
            ),
        )
        listening, normalized = normalize(pcm, config.loudness_lufs, config.true_peak_dbtp)
        result = dict(
            contract="audio-sample-result/1",
            cell_id=attempt["cell_id"],
            take=attempt["take"],
            provider_audio_blob=audio_blob,
            decoded_pcm_blob=pcm_blob,
            blind_audio_blob=journal.blob(listening, "audio/wav"),
            metrics=metrics,
            normalization=normalized,
            cost=dict(
                estimate(profile, audio.usage, audio.usage_basis),
                provider_reported_headers={
                    k: v
                    for k, v in response.headers.items()
                    if k in ("character-cost", "x-character-count", "x-characters-used")
                },
            ),
            receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            decoded_ref=journal.ref(output_key + ":decoded").model_dump(mode="json"),
            production_ready=False,
        )
        journal.put(output_key + ":result", result)
        return "completed"
    except (ContractError, ValueError, KeyError, TypeError) as error:
        journal.put(
            output_key + ":failure",
            dict(
                contract="audio-failure/1",
                status="processing_failed" if phase == "media_processing" else "response_invalid",
                phase=phase,
                reason=str(error),
                receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
            ),
        )
        return "processing_failed" if phase == "media_processing" else "response_invalid"
