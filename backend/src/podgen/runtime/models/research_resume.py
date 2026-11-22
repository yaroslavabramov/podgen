"""Restore one retained paid turn, rather than re-executing its completed prefix."""

import json
from dataclasses import dataclass

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict


@dataclass(frozen=True)
class RetainedTurn:
    index: int
    body: dict


def retained_turn(journal, root, ledger, history, *, minimum):
    candidates = [
        index
        for index, row in ledger.rows.items()
        if index >= minimum and row["status"] == "response_completed" and index in history.responses
    ]
    if not candidates:
        return None
    index = max(candidates)
    row = ledger.rows[index]
    event = history.provider_response(index)
    if event["invocation_id"] != str(root["invocation_id"]) or event["data"].get("source_turn_id") != str(
        row["invocation_id"]
    ):
        raise Conflict("research_resume_receipt_owner_mismatch")
    reference = RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"])
    with journal.read_operation():
        record = journal.read(reference)
        body = json.loads(journal.read_blob(reference, BlobRef.model_validate(record["request_blob"])))
    metadata = body.get("metadata", {})
    if metadata.get("podgen_source_turn") != str(index) or metadata.get("podgen_invocation_id") != str(
        row["invocation_id"]
    ):
        raise Conflict("research_resume_request_owner_mismatch")
    # This is the existing operation-local snapshot, not a permission cache.
    # stage_prepare and invoke_once still perform their ordinary authorization.
    journal.execution.remember_physical(root["run_id"], root["invocation_id"], row, (body, record))
    return RetainedTurn(index, body)
