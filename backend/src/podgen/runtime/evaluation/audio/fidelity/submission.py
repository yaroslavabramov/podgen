from datetime import datetime, timezone

from podgen.domain.errors import Conflict

from .contracts import prefix


def submit(journal, config, manifest, delivery, submitted, policy, credential, transport):
    root = prefix(config.audit_id)
    key = delivery["key"]
    claim_key = "recognition-subject:" + str(config.subject_ref.record_id)
    claim = dict(audit_manifest_ref=journal.ref(root + ":manifest").model_dump(mode="json"))
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
            request_number=submitted + 1,
            max_requests=policy["max_total_requests"],
            max_cost_usd=policy["max_total_cost_usd"],
            retry_authorization_ref=journal.ref(key + ":authorization").model_dump(mode="json")
            if delivery["authorization"]
            else None,
        ),
    )
    response = transport.submit(
        manifest["request"], journal.bytes(root + ":manifest", manifest["wire_blob"]), credential
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
