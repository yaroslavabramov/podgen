"""Explicit local readmission of a saved reading response under its successor admission contract.

The parent's exact paid response is admitted again without a provider call. The rejected
attempt, its cost and its rejection remain unchanged; the run manifest keeps its pin.
"""

from copy import deepcopy
from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.writing.reading_audit.consistency import ADMISSIBILITY_VERSION, KEY, RETAINED_EVIDENCE_VERSION

from .contracts import RunFailure
from .reading_workflow import is_reading
from .rejected_output import saved_text

KIND = "local_readmission"
AUTHORIZATION = "local-readmission/1"
# A rejection is readmissible only under the successor contract that owns its repair.
SUCCESSORS = {("audit_retained_evidence_missing", ADMISSIBILITY_VERSION): RETAINED_EVIDENCE_VERSION}


def _request_record(journal, row):
    return journal.read(RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"]))


def _authorization(journal, target):
    def refuse():
        raise RunFailure(
            "local_readmission_not_eligible", run_id=target["run_id"], invocation_id=target["invocation_id"]
        )

    if (
        not is_reading(target["stage"])
        or target["status"] != "validation_failed"
        or target["attempt_kind"] == KIND
        or target["operation_kind"] not in ("role", "anchor_repair")
    ):
        refuse()
    failures = [event for event in journal.events(target) if event["event_kind"] == "output_validation_error"]
    if len(failures) != 1:
        refuse()
    failure_ref = RuntimeRef(record_kind="tool_execution", record_id=failures[0]["record_id"])
    code = journal.read(failure_ref)["data"].get("code")
    pinned = journal.manifest(target["run_id"]).get(KEY)
    successor = SUCCESSORS.get((code, pinned))
    if successor is None:
        refuse()
    saved_text(journal, target, code="local_readmission_not_eligible")
    return dict(
        kind=AUTHORIZATION,
        actor_id=str(journal.repo.actor_id),
        target_invocation_id=str(target["invocation_id"]),
        failure_event_ref=failure_ref.model_dump(mode="json"),
        failure_code=code,
        pinned_admission_contract=pinned,
        admission_contract=successor,
        maximum_new_attempts=1,
    )


def _metadata(original, child_id, index, parent_id):
    return dict(
        original["metadata"],
        podgen_invocation_id=str(child_id),
        podgen_attempt_index=str(index),
        podgen_parent_invocation_id=str(parent_id),
    )


def validate(journal, child):
    """Re-derive the grant from the immutable parent; return that parent."""
    parent = next(
        (
            row
            for row in journal.attempts(child["run_id"], child["stage"])
            if row["invocation_id"] == child["parent_invocation_id"]
        ),
        None,
    )
    if parent is None:
        raise Conflict("local_readmission_origin_missing")
    record, original = _request_record(journal, child), journal.request(parent)
    if (
        record.get("authorization") != _authorization(journal, parent)
        or record["context"] != _request_record(journal, parent)["context"]
        or child["invocation_id"] != uuid5(parent["invocation_id"], "local-readmission:1")
        or child["operation_kind"] != parent["operation_kind"]
        or journal.request(child)
        != dict(
            original,
            metadata=_metadata(
                original, child["invocation_id"], child["attempt_index"], parent["invocation_id"]
            ),
        )
    ):
        raise Conflict("local_readmission_authorization_mismatch")
    return parent


def authorize(journal, command, target_id, *, guard, allow_create=True):
    found = [
        (rows, row)
        for stage in journal.manifest(command.run_id)["stages"]
        if (rows := journal.attempts(command.run_id, stage))
        for row in rows
        if row["invocation_id"] == target_id
    ]
    if not found:
        raise RunFailure("local_readmission_not_eligible", run_id=command.run_id)
    rows, target = found[0]
    child = next(
        (row for row in rows if row["attempt_kind"] == KIND and row["parent_invocation_id"] == target_id),
        None,
    )
    if child is not None:
        validate(journal, child)
        return child
    authorization = _authorization(journal, target)
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("local_readmission_not_current", run_id=command.run_id, invocation_id=target_id)
    guard()
    identity = uuid5(target_id, "local-readmission:1")
    index = target["attempt_index"] + 1
    original = journal.request(target)
    return journal.prepare(
        command.run_id,
        target["stage"],
        dict(deepcopy(original), metadata=_metadata(original, identity, index, target_id)),
        deepcopy(_request_record(journal, target)["context"]),
        attempt_index=index,
        parent_invocation_id=target_id,
        attempt_kind=KIND,
        operation_kind=target["operation_kind"],
        authorization=authorization,
    )


def saved_output(journal, invocation):
    """The parent's exact paid response text; this attempt never reaches a provider."""
    if invocation["status"] != "prepared":
        raise RunFailure(
            "local_readmission_action_used",
            run_id=invocation["run_id"],
            invocation_id=invocation["invocation_id"],
        )
    text, _, _ = saved_text(journal, validate(journal, invocation), code="local_readmission_not_eligible")
    return text


def admission_contract(journal, invocation, pinned):
    if invocation["attempt_kind"] != KIND:
        return pinned
    validate(journal, invocation)
    return _request_record(journal, invocation)["authorization"]["admission_contract"]
