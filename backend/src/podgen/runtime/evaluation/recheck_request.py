"""Resolve a repaired review to its actual semantic request, through immutable attempts."""

import json

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

from .attempts import validate_lineage


def _ref(row):
    return RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"]).model_dump(
        mode="json"
    )


def _record(journal, row, run_id, stage):
    record = journal.read(RuntimeRef.model_validate(_ref(row)))
    body = journal.request(row)
    if (
        row["run_id"] != run_id
        or row["stage"] != stage
        or record.get("contract") != "model-request-v1"
        or record.get("run_id") != str(run_id)
        or record.get("stage") != stage
        or record.get("invocation_id") != str(row["invocation_id"])
        or record.get("operation_kind") != row["operation_kind"]
        or record.get("attempt_kind") != row["attempt_kind"]
        or body.get("metadata", {}).get("podgen_run_id") != str(run_id)
        or body.get("metadata", {}).get("podgen_stage") != stage
        or body.get("metadata", {}).get("podgen_invocation_id") != str(row["invocation_id"])
    ):
        raise Conflict("revision_verification_request_lineage_mismatch")
    return record, body


def semantic_request(journal, run_id, stage, invocation, context, script, output):
    """Do not treat an address-selection request as a new editorial commission."""
    if invocation["run_id"] != run_id or invocation["stage"] != stage:
        raise Conflict("revision_verification_request_lineage_mismatch")
    if invocation["operation_kind"] == "role":
        return journal.request(invocation)
    if invocation["operation_kind"] != "anchor_repair":
        raise Conflict("revision_verification_request_lineage_mismatch")
    rows = journal.attempts(run_id, stage)
    validate_lineage(rows, stage)
    by_id = {row["invocation_id"]: row for row in rows}
    if (
        len(by_id) != len(rows)
        or len({row["attempt_index"] for row in rows}) != len(rows)
        or by_id.get(invocation["invocation_id"]) != invocation
    ):
        raise Conflict("revision_verification_request_lineage_mismatch")
    chain, seen, row = [], set(), invocation
    while True:
        if row["invocation_id"] in seen:
            raise Conflict("revision_verification_request_lineage_mismatch")
        seen.add(row["invocation_id"])
        record, body = _record(journal, row, run_id, stage)
        chain.append((row, record, body))
        parent = by_id.get(row["parent_invocation_id"])
        if row["parent_invocation_id"] is None:
            if row["attempt_index"] != 0 or record.get("parent_request_ref") is not None:
                raise Conflict("revision_verification_request_lineage_mismatch")
            break
        if (
            parent is None
            or parent["attempt_index"] >= row["attempt_index"]
            or record.get("parent_request_ref") != _ref(parent)
        ):
            raise Conflict("revision_verification_request_lineage_mismatch")
        row = parent
    if chain[0][1]["context"] != context:
        raise Conflict("revision_verification_request_lineage_mismatch")
    for (child, saved, body), (parent, previous, parent_body) in zip(chain[:-1], chain[1:], strict=True):
        if child["operation_kind"] == parent["operation_kind"]:
            if saved["context"] != previous["context"] or {
                k: v for k, v in body.items() if k != "metadata"
            } != {k: v for k, v in parent_body.items() if k != "metadata"}:
                raise Conflict("revision_verification_request_lineage_mismatch")
        elif (
            child["attempt_kind"] != "anchor_repair"
            or child["operation_kind"] != "anchor_repair"
            or parent["operation_kind"] != "role"
            or saved["context"].get("parent_request_ref") != _ref(parent)
            or saved["context"].get("draft_ref") != previous["context"]["script_ref"]
            or any(saved["context"].get(k) != v for k, v in previous["context"].items())
        ):
            raise Conflict("revision_verification_request_lineage_mismatch")
    original = next((item for item in chain if item[0]["operation_kind"] == "role"), None)
    if original is None:
        raise Conflict("revision_verification_request_lineage_mismatch")

    # Replay the existing deterministic repair against the rejected role response,
    # including its saved plan, selectors and choices. Editorial prose is unchanged.
    from .review_repair import apply_anchor_repair

    completed = journal.output(invocation)
    if (
        completed["request_ref"] != _ref(invocation)
        or completed["output"] != output
        or completed.get("repair_choices") is None
    ):
        raise Conflict("revision_verification_repaired_output_mismatch")
    repaired, _ = apply_anchor_repair(
        journal, invocation, json.dumps(completed["repair_choices"], ensure_ascii=False), draft=script
    )
    if repaired != output:
        raise Conflict("revision_verification_repaired_output_mismatch")
    return original[2]
