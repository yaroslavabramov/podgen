"""Independent durable ASR exchange, keyed by full contextual PCM, then exact extraction."""

import os
from datetime import datetime, timezone
from decimal import Decimal

from podgen.audio.media.recognition import lossless_flac
from podgen.audio.recognition.contracts import RecognitionProfile
from podgen.audio.recognition.fish import prepare
from podgen.domain.errors import ContractError

from ..exchange_history import attempts
from .attempts import state
from .contracts import prefix
from .extraction import extract
from .localization_state import binding, proposal_ledger
from .localization_state import current as current


def run(journal, proposal_id, transport, *, retry_reason=None, reprocess_reason=None):
    with journal.lock():
        proposal, approval, take, root = binding(journal, proposal_id)
        payload, budget = proposal["payload"], approval.localization_budget
        take_key = take["key"] + ":result"
        audio = journal.bytes(take_key, take["result"]["decoded_pcm_blob"])
        manifest = journal.get(root + ":manifest")
        if manifest is None:
            flac, proof = lossless_flac(audio)
            request, wire = prepare(
                RecognitionProfile.model_validate(payload["localization"]["recognition"]), flac
            )
            manifest = dict(
                contract="audio-local-localization-input/1",
                proposal_hash=proposal["proposal_hash"],
                contextual_audio_blob=take["result"]["decoded_pcm_blob"],
                take_ref=journal.ref(take_key).model_dump(mode="json"),
                transport_audio_blob=journal.blob(flac, "audio/flac"),
                transport_proof=proof,
                request=request,
                wire_blob=journal.blob(wire, "application/octet-stream"),
            )
            journal.put(root + ":manifest", manifest)
        if journal.get(root + ":result"):
            return "extraction_ready"
        rows = attempts(journal, root)
        if retry_reason is not None:
            if not retry_reason.strip() or not rows or state(journal, rows[-1]["key"]) != "provider_rejected":
                raise ContractError("local_replacement_asr_retry_requires_known_rejection")
            key = f"{root}:attempt:{len(rows) + 1}"
        else:
            key = rows[-1]["key"] if rows else root + ":attempt:1"
        status = state(journal, key)
        if reprocess_reason is not None and (not reprocess_reason.strip() or status != "receipt_saved"):
            raise ContractError("local_replacement_asr_reprocess_requires_saved_success")
        if status == "prepared":
            count, cost = proposal_ledger(journal, proposal_id)
            reservation = Decimal(budget.reservation_per_request_usd)
            price = Decimal(payload["localization"]["price"]["usd_per_audio_minute"])
            expected = (
                Decimal(manifest["transport_proof"]["sample_frames"])
                / Decimal(manifest["transport_proof"]["sample_rate_hz"])
                / 60
                * price
            )
            if (
                reservation < expected
                or count >= budget.max_requests
                or cost + reservation > Decimal(budget.max_cost_usd)
            ):
                return "asr_budget_exhausted"
            credential = os.environ.get(payload["localization"]["credential_env"])
            if not credential:
                return "asr_credential_missing"
            journal.put(
                key + ":prepared",
                dict(
                    contract="audio-local-asr-attempt/1",
                    reason=retry_reason,
                    manifest_ref=journal.ref(root + ":manifest").model_dump(mode="json"),
                ),
            )
            journal.put(
                key + ":submit",
                dict(
                    contract="audio-local-asr-submit/1",
                    approval_ref=journal.ref(prefix(proposal_id) + ":approval").model_dump(mode="json"),
                    reservation_usd=str(reservation),
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                    delivery_semantics="local_at_most_once_no_provider_idempotency_guarantee",
                ),
            )
            response = transport.submit(
                manifest["request"], journal.bytes(root + ":manifest", manifest["wire_blob"]), credential
            )
            journal.put(
                key + ":receipt",
                dict(
                    contract="audio-local-asr-receipt/1",
                    status_code=response.status_code,
                    headers=response.headers,
                    elapsed_ms=response.elapsed_ms,
                    complete=response.complete,
                    error=response.error,
                    raw_response_blob=journal.blob(response.body, "application/json"),
                ),
            )
            status = state(journal, key)
        if status != "receipt_saved":
            return "asr_" + status
        if journal.get(root + ":failure") and reprocess_reason is None and retry_reason is None:
            return "localization_failed"
        return extract(journal, proposal, root, key, audio, reprocess_reason)
