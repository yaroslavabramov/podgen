import os
from datetime import datetime, timezone
from decimal import Decimal

from podgen.domain.errors import Conflict, ContractError

from .contracts import prefix
from .prepare import prepare
from .processing import process, revisions


def state(journal, config):
    key = prefix(config.audit_id)
    receipts = journal.get(key + ":receipt")
    processed = revisions(journal, config.audit_id)
    if processed and processed[-1]["result"]:
        return "candidates_generated"
    if processed and processed[-1]["failure"]:
        return processed[-1]["failure"]["status"]
    if receipts:
        if not receipts["complete"] or receipts["error"]:
            return "outcome_unknown"
        if not 200 <= receipts["status_code"] < 300:
            return "provider_rejected"
        return "receipt_saved"
    return "outcome_unknown" if journal.get(key + ":submit") else "prepared"


def run(journal, config, transport):
    with journal.lock():
        manifest = prepare(journal, config)
        key = prefix(config.audit_id)
        current = state(journal, config)
        if current not in ("prepared", "receipt_saved"):
            return current
        if current == "prepared":
            credential = os.environ.get(config.credential_env)
            if not credential:
                return "credential_missing"
            submitted = 1 if journal.get(key + ":submit") else 0
            reserved = Decimal(config.reservation_usd) * (submitted + 1)
            if submitted >= config.max_requests or reserved > Decimal(config.max_cost_usd):
                return "budget_exhausted"
            claim_key = "recognition-subject:" + str(config.subject_ref.record_id)
            claim = dict(audit_manifest_ref=journal.ref(key + ":manifest").model_dump(mode="json"))
            if journal.get(claim_key) not in (None, claim):
                raise Conflict("recognition_subject_already_requested_use_original_audit")
            journal.put(claim_key, claim)
            journal.put(
                key + ":submit",
                dict(
                    contract="audio-recognition-submit-intent/1",
                    manifest_ref=claim["audit_manifest_ref"],
                    submitted_at=datetime.now(timezone.utc).isoformat(),
                    reservation_usd=config.reservation_usd,
                    request_number=1,
                    max_requests=config.max_requests,
                    max_cost_usd=config.max_cost_usd,
                ),
            )
            response = transport.submit(
                manifest["request"], journal.bytes(key + ":manifest", manifest["wire_blob"]), credential
            )
            journal.put(
                key + ":receipt",
                dict(
                    contract="audio-recognition-http-receipt/1",
                    submit_ref=journal.ref(key + ":submit").model_dump(mode="json"),
                    status_code=response.status_code,
                    headers=response.headers,
                    raw_response_blob=journal.blob(
                        response.body, response.headers.get("content-type", "application/octet-stream")
                    ),
                    elapsed_ms=response.elapsed_ms,
                    complete=response.complete,
                    error=response.error,
                ),
            )
            current = state(journal, config)
            if current != "receipt_saved":
                return current
        return process(journal, config, manifest)


def reprocess(journal, config, reason):
    if not reason or not reason.strip():
        raise ContractError("fidelity_reprocess_reason_required")
    with journal.lock():
        manifest = prepare(journal, config)
        receipt = journal.get(prefix(config.audit_id) + ":receipt")
        if (
            not receipt
            or not receipt["complete"]
            or receipt["error"]
            or not 200 <= receipt["status_code"] < 300
        ):
            raise ContractError("fidelity_reprocess_requires_complete_success_receipt")
        return process(journal, config, manifest, reason=reason)
