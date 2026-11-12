import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal

from podgen.audio.media.processing import tools
from podgen.audio.synthesis.contracts import PreparedRequest
from podgen.audio.synthesis.transport import encode_request
from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError

from ..exchange_history import attempts
from ..subject_contracts import AudioSubject
from ..subjects import audio_subject
from .approval import authorized
from .attempts import ledger, state
from .contracts import prefix
from .processing import process


def synthesize(journal, proposal_id, transport, *, retry_reason=None):
    with journal.lock():
        proposal, approval = authorized(journal, proposal_id)
        payload, root = proposal["payload"], prefix(proposal_id)
        if payload["tool_versions"] != tools():
            raise ContractError("local_replacement_tools_changed_prepare_new_proposal")
        rows = attempts(journal, root)
        if retry_reason is not None:
            if not retry_reason.strip() or not rows or state(journal, rows[-1]["key"]) != "provider_rejected":
                raise ContractError("local_replacement_retry_requires_known_rejection_and_reason")
            key = f"{root}:attempt:{len(rows) + 1}"
        else:
            key = rows[-1]["key"] if rows else root + ":attempt:1"
        current = state(journal, key)
        if current == "prepared":
            count, cost = ledger(journal, root)
            if count >= approval.max_requests or cost + Decimal(
                approval.reservation_per_request_usd
            ) > Decimal(approval.max_cost_usd):
                return "budget_exhausted"
            credential = os.environ.get(payload["profile"]["credential_env"])
            if not credential:
                return "credential_missing"
            request = PreparedRequest.model_validate(payload["request"])
            parent_binding = AudioSubject.model_validate(payload["parent_subject"])
            parent = audio_subject(journal, parent_binding)
            audio = journal.base.read_blob(parent_binding.ref, BlobRef.model_validate(parent["audio_blob"]))
            if (
                parent["audio_blob"]["sha256"] != payload["parent_audio_sha256"]
                or hashlib.sha256(audio).hexdigest() != payload["parent_audio_sha256"]
            ):
                raise ContractError("local_replacement_parent_audio_changed_before_submit")
            if encode_request(request) != journal.bytes(root + ":proposal", payload["wire_blob"]):
                raise ContractError("local_replacement_frozen_wire_mismatch")
            if journal.get(key + ":prepared") is None:
                journal.put(
                    key + ":prepared",
                    dict(
                        contract="audio-local-replacement-attempt/1",
                        proposal_ref=journal.ref(root + ":proposal").model_dump(mode="json"),
                        proposal_hash=proposal["proposal_hash"],
                        reason=retry_reason,
                        parent_attempt_ref=journal.ref(rows[-1]["key"] + ":prepared").model_dump(mode="json")
                        if rows
                        else None,
                    ),
                )
            journal.put(
                key + ":submit",
                dict(
                    contract="audio-local-replacement-submit-intent/1",
                    approval_ref=journal.ref(root + ":approval").model_dump(mode="json"),
                    prepared_ref=journal.ref(key + ":prepared").model_dump(mode="json"),
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                    reservation_usd=approval.reservation_per_request_usd,
                    delivery_semantics="local_at_most_once_no_provider_idempotency_guarantee",
                ),
            )
            response = transport.submit(request, credential)
            journal.put(
                key + ":receipt",
                dict(
                    contract="audio-local-replacement-http-receipt/1",
                    status_code=response.status_code,
                    headers=response.headers,
                    elapsed_ms=response.elapsed_ms,
                    complete=response.complete,
                    error=response.error,
                    raw_response_blob=journal.blob(
                        response.body, response.headers.get("content-type", "application/octet-stream")
                    ),
                ),
            )
            current = state(journal, key)
        return process(journal, proposal, key) if current == "receipt_saved" else current


def reprocess(journal, proposal_id, reason):
    if not reason.strip():
        raise ContractError("local_replacement_reprocess_reason_required")
    with journal.lock():
        proposal, _ = authorized(journal, proposal_id)
        rows = attempts(journal, prefix(proposal_id))
        if (
            not rows
            or not rows[-1]["receipt"]
            or not rows[-1]["receipt"]["complete"]
            or rows[-1]["receipt"]["error"]
            or not 200 <= rows[-1]["receipt"]["status_code"] < 300
        ):
            raise ContractError("local_replacement_reprocess_requires_complete_success")
        return process(journal, proposal, rows[-1]["key"], reason)
