"""Decode saved responses; media failures are distinct from uncertain archive publication."""

from podgen.audio.media.processing import VERSION, decode, normalize, tools
from podgen.audio.synthesis.registry import adapter
from podgen.domain.errors import ContractError

from .costs import estimate
from .descriptors import ProcessingResult

ASSEMBLY_INPUT = "ffmpeg-assembly-input/1"
MEDIA_ERRORS = (ContractError, ValueError, KeyError, TypeError)


def processing_contract(config):
    return getattr(config, "processing_contract", VERSION)


def process(journal, key, profile, config, request, response, **options):
    return process_result(journal, key, profile, config, request, response, **options).status


def process_result(
    journal,
    key,
    profile,
    config,
    request,
    response,
    *,
    output_key=None,
    reason=None,
    attempt=None,
    processing=None,
):
    output_key = output_key or key
    attempt = attempt if attempt is not None else journal.get(key + ":prepared")
    processing = processing or dict(
        contract="audio-processing-attempt/1",
        processing_contract=processing_contract(config),
        tool_versions=tools(),
        source_receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
        reason=reason,
    )
    journal.put(output_key + ":processing", processing)
    try:
        audio = adapter(profile.synthesis.provider).decode(request, response)
    except MEDIA_ERRORS as error:
        return _failure(journal, key, output_key, processing, "provider_decode", error)
    audio_blob = journal.blob(audio.audio, audio.media_type)
    audio_value = dict(
        contract="audio-provider-output/1",
        provider_audio_blob=audio_blob,
        encoding=audio.encoding,
        sample_rate_hz=audio.sample_rate_hz,
        request_id=audio.request_id,
        usage=audio.usage,
        usage_basis=audio.usage_basis,
        alignment=audio.alignment,
        receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
    )
    journal.put(key + ":audio", audio_value)
    try:
        pcm, metrics = decode(audio)
    except MEDIA_ERRORS as error:
        return _failure(journal, key, output_key, processing, "media_processing", error, audio_value)
    pcm_blob = journal.blob(pcm, "audio/wav")
    decoded_value = dict(
        contract="audio-decoded-output/1",
        pcm_blob=pcm_blob,
        metrics=metrics,
        provider_output_ref=journal.ref(key + ":audio").model_dump(mode="json"),
    )
    journal.put(output_key + ":decoded", decoded_value)
    try:
        listening, normalized = _playback(pcm, metrics, config)
    except MEDIA_ERRORS as error:
        return _failure(
            journal, key, output_key, processing, "media_processing", error, audio_value, decoded_value
        )
    listening_blob = pcm_blob if listening is None else journal.blob(listening, "audio/wav")
    result = dict(
        contract="audio-sample-result/1",
        cell_id=attempt["cell_id"],
        take=attempt["take"],
        provider_audio_blob=audio_blob,
        decoded_pcm_blob=pcm_blob,
        blind_audio_blob=listening_blob,
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
    # Publication errors propagate. A committed result with a lost ACK must
    # invalidate the operation, not be relabelled as a failed media transform.
    journal.put(output_key + ":result", result)
    return ProcessingResult("completed", output_key, processing, audio_value, decoded_value, result)


def _playback(pcm, metrics, config):
    if processing_contract(config) == ASSEMBLY_INPUT:
        return None, dict(
            contract=ASSEMBLY_INPUT,
            normalization_type="none",
            applied_gain_db=0,
            integrated_lufs=metrics["integrated_lufs"],
            true_peak_dbtp=metrics["true_peak_dbtp"],
            sample_rate_hz=metrics["sample_rate_hz"],
            channels=metrics["channels"],
            codec="pcm_s32le",
        )
    return normalize(pcm, config.loudness_lufs, config.true_peak_dbtp)


def _failure(journal, key, output_key, processing, phase, error, audio=None, decoded=None):
    status = "processing_failed" if phase == "media_processing" else "response_invalid"
    failure = dict(
        contract="audio-failure/1",
        status=status,
        phase=phase,
        reason=str(error),
        receipt_ref=journal.ref(key + ":receipt").model_dump(mode="json"),
    )
    journal.put(output_key + ":failure", failure)
    return ProcessingResult(status, output_key, processing, audio, decoded, failure=failure)
