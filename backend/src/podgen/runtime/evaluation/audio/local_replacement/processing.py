from podgen.audio.media.processing import decode, tools
from podgen.audio.synthesis.contracts import HttpReceipt, PreparedRequest
from podgen.audio.synthesis.registry import adapter
from podgen.domain.errors import ContractError

from ..contracts import VoiceCandidate
from ..costs import estimate
from ..exchange_history import revisions


def process(journal, proposal, attempt_key, reason=None):
    prior = revisions(journal, attempt_key)
    key = f"{attempt_key}:processing:{len(prior) + 1}"
    journal.put(
        key,
        dict(
            contract="audio-local-replacement-processing/1",
            tool_versions=tools(),
            reason=reason,
            receipt_ref=journal.ref(attempt_key + ":receipt").model_dump(mode="json"),
        ),
    )
    payload, receipt = proposal["payload"], journal.get(attempt_key + ":receipt")
    try:
        request = PreparedRequest.model_validate(payload["request"])
        response = HttpReceipt(
            receipt["status_code"],
            receipt["headers"],
            journal.bytes(attempt_key + ":receipt", receipt["raw_response_blob"]),
            receipt["elapsed_ms"],
            receipt["complete"],
            receipt["error"],
        )
        audio = adapter(request.provider).decode(request, response)
        decoded, metrics = decode(audio)
        result = dict(
            contract="audio-local-replacement-take/1",
            proposal_hash=proposal["proposal_hash"],
            decoded_pcm_blob=journal.blob(decoded, "audio/wav"),
            provider_audio_blob=journal.blob(audio.audio, audio.media_type),
            metrics=metrics,
            request_id=audio.request_id,
            usage=audio.usage,
            usage_basis=audio.usage_basis,
            provider_alignment=audio.alignment,
            cost=estimate(VoiceCandidate.model_validate(payload["profile"]), audio.usage, audio.usage_basis),
            receipt_ref=journal.ref(attempt_key + ":receipt").model_dump(mode="json"),
            production_ready=False,
        )
        journal.put(key + ":result", result)
        return "synthesized"
    except (ContractError, ValueError, KeyError, TypeError) as error:
        journal.put(
            key + ":failure", dict(contract="audio-local-replacement-processing-failure/1", reason=str(error))
        )
        return "processing_failed"
