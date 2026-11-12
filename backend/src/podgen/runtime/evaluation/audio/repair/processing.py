from podgen.audio.media.processing import VERSION, decode, tools
from podgen.audio.synthesis.contracts import HttpReceipt, PreparedRequest
from podgen.audio.synthesis.registry import adapter
from podgen.domain.errors import ContractError

from ..contracts import VoiceCandidate
from ..costs import estimate
from ..exchange_history import revisions
from .contracts import prefix


def process(journal, config, manifest, attempt_key, reason=None):
    previous = revisions(journal, attempt_key)
    key = f"{attempt_key}:processing:{len(previous) + 1}"
    journal.put(
        key,
        dict(
            contract="fragment-repair-processing/1",
            processing_contract=VERSION,
            tool_versions=tools(),
            reason=reason,
            receipt_ref=journal.ref(attempt_key + ":receipt").model_dump(mode="json"),
            parent_processing_ref=journal.ref(previous[-1]["key"]).model_dump(mode="json")
            if previous
            else None,
        ),
    )
    receipt = journal.get(attempt_key + ":receipt")
    try:
        profile = VoiceCandidate.model_validate(manifest["profile"])
        request = PreparedRequest.model_validate(manifest["original_request"])
        response = HttpReceipt(
            receipt["status_code"],
            receipt["headers"],
            journal.bytes(attempt_key + ":receipt", receipt["raw_response_blob"]),
            receipt["elapsed_ms"],
            receipt["complete"],
            receipt["error"],
        )
        audio = adapter(profile.synthesis.provider).decode(request, response)
        original = journal.blob(audio.audio, audio.media_type)
        journal.put(
            key + ":provider-output",
            dict(
                contract="fragment-repair-provider-output/1",
                audio_blob=original,
                encoding=audio.encoding,
                sample_rate_hz=audio.sample_rate_hz,
                usage=audio.usage,
                usage_basis=audio.usage_basis,
                request_id=audio.request_id,
                alignment=audio.alignment,
                receipt_ref=journal.ref(attempt_key + ":receipt").model_dump(mode="json"),
            ),
        )
        pcm, metrics = decode(audio)
        pcm_blob = journal.blob(pcm, "audio/wav")
        journal.put(
            key + ":decoded",
            dict(
                contract="fragment-repair-decoded/1",
                pcm_blob=pcm_blob,
                metrics=metrics,
                provider_output_ref=journal.ref(key + ":provider-output").model_dump(mode="json"),
            ),
        )
        journal.put(
            key + ":result",
            dict(
                contract="audio-evaluation-repair-part/1",
                repair_id=str(config.repair_id),
                manifest_ref=journal.ref(prefix(config.repair_id) + ":manifest").model_dump(mode="json"),
                source_ref=manifest["source_ref"],
                decoded_ref=journal.ref(key + ":decoded").model_dump(mode="json"),
                decoded_pcm_blob=pcm_blob,
                provider_audio_blob=original,
                metrics=metrics,
                cost=estimate(profile, audio.usage, audio.usage_basis),
                evidence=manifest["evidence"],
                parent_subject=manifest["parent_subject"],
                parent_part=manifest["parent_part"],
                repair_depth=manifest["repair_depth"],
                asr_and_audio_review="required_after_reassembly",
                production_ready=False,
            ),
        )
        return "part_generated"
    except (ContractError, ValueError, KeyError, TypeError) as error:
        journal.put(
            key + ":failure",
            dict(
                contract="fragment-repair-processing-failure/1",
                status="processing_failed",
                reason=str(error),
                receipt_ref=journal.ref(attempt_key + ":receipt").model_dump(mode="json"),
            ),
        )
        return "processing_failed"
