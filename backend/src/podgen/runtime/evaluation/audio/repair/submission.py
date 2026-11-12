from datetime import datetime, timezone

from podgen.audio.synthesis.contracts import PreparedRequest
from podgen.audio.synthesis.transport import encode_request
from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict, ContractError

from .contracts import prefix


def submit(journal, config, manifest, key, rows, credential, transport, reason):
    root = prefix(config.repair_id)
    claim_key = "fragment-repair-claim:" + digest(
        dict(
            parent=config.parent_subject.model_dump(mode="json"),
            part=config.part_source.model_dump(mode="json"),
        )
    )
    claim = dict(manifest_ref=journal.ref(root + ":manifest").model_dump(mode="json"))
    if journal.get(claim_key) not in (None, claim):
        raise Conflict("repair_already_requested_for_parent_fragment_use_original_repair")
    journal.put(claim_key, claim)
    request = PreparedRequest.model_validate(manifest["original_request"])
    frozen = journal.bytes(root + ":manifest", manifest["wire_blob"])
    if encode_request(request) != frozen:
        raise ContractError("repair_wire_changed")
    if journal.get(key + ":prepared") is None:
        journal.put(
            key + ":prepared",
            dict(
                contract="fragment-repair-attempt/1",
                manifest_ref=claim["manifest_ref"],
                reason=reason or config.reason,
                parent_attempt_ref=journal.ref(rows[-1]["key"] + ":prepared").model_dump(mode="json")
                if rows
                else None,
            ),
        )
    journal.put(
        key + ":submit",
        dict(
            contract="fragment-repair-submit-intent/1",
            prepared_ref=journal.ref(key + ":prepared").model_dump(mode="json"),
            submitted_at=datetime.now(timezone.utc).isoformat(),
            reservation_usd=config.reservation_per_request_usd,
        ),
    )
    response = transport.submit(request, credential)
    journal.put(
        key + ":receipt",
        dict(
            contract="fragment-repair-http-receipt/1",
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
