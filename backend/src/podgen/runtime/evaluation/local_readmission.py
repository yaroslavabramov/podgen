"""Explicit local readmission of a saved reading response under its successor admission contract.

The parent's exact paid response is admitted again without a provider call. The rejected
attempt, its cost and its rejection remain unchanged; the run manifest keeps its pin.
"""

from dataclasses import dataclass
from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.private_journal.attempt_projection import stage_for_attempt
from podgen.writing.reading_audit.consistency import ADMISSIBILITY_VERSION, KEY, RETAINED_EVIDENCE_VERSION

from .contracts import RunFailure
from .reading_workflow import is_reading
from .rejected_output import saved_text

KIND = "local_readmission"
LEGACY_AUTHORIZATION = "local-readmission/1"
AUTHORIZATION = "local-readmission/2"
# A rejection is readmissible only under the successor contract that owns its repair.
SUCCESSORS = {("audit_retained_evidence_missing", ADMISSIBILITY_VERSION): RETAINED_EVIDENCE_VERSION}


def _request_record(journal, row):
    return journal.read(RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"]))


@dataclass(frozen=True)
class RejectedReading:
    request: dict
    context: dict
    text: str
    authorization: dict


def _rejected(journal, target, grant_version=AUTHORIZATION):
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
    events = journal.events(target)
    failures = [event for event in events if event["event_kind"] == "output_validation_error"]
    if len(failures) != 1:
        refuse()
    failure_ref = RuntimeRef(record_kind="tool_execution", record_id=failures[0]["record_id"])
    code = journal.read(failure_ref)["data"].get("code")
    manifest = journal.manifest(target["run_id"])
    if target["stage"] not in manifest["stages"]:
        refuse()
    pinned = manifest.get(KEY)
    successor = SUCCESSORS.get((code, pinned))
    if successor is None:
        refuse()
    text, request, _ = saved_text(journal, target, code="local_readmission_not_eligible", events=events)
    authorization = dict(
        kind=grant_version,
        actor_id=str(journal.repo.actor_id),
        target_invocation_id=str(target["invocation_id"]),
        failure_event_ref=failure_ref.model_dump(mode="json"),
        failure_code=code,
        pinned_admission_contract=pinned,
        admission_contract=successor,
        maximum_new_attempts=1,
    )
    return RejectedReading(request, _request_record(journal, target)["context"], text, authorization)


def _metadata(original, child_id, index, parent_id):
    return dict(
        original["metadata"],
        podgen_invocation_id=str(child_id),
        podgen_attempt_index=str(index),
        podgen_parent_invocation_id=str(parent_id),
    )


def _verified(journal, child, parent):
    if parent is None:
        raise Conflict("local_readmission_origin_missing")
    record = _request_record(journal, child)
    grant_version = (record.get("authorization") or {}).get("kind")
    if grant_version not in (LEGACY_AUTHORIZATION, AUTHORIZATION):
        raise Conflict("local_readmission_authorization_mismatch")
    source = _rejected(journal, parent, grant_version)
    if (
        record.get("authorization") != source.authorization
        or record["context"] != source.context
        or child["invocation_id"] != uuid5(parent["invocation_id"], "local-readmission:1")
        or child["operation_kind"] != parent["operation_kind"]
        or journal.request(child)
        != dict(
            source.request,
            metadata=_metadata(
                source.request, child["invocation_id"], child["attempt_index"], parent["invocation_id"]
            ),
        )
    ):
        raise Conflict("local_readmission_authorization_mismatch")
    return source


def _verify_parent(journal, child):
    with journal.read_operation():
        rows = journal.attempts(child["run_id"], child["stage"])
        parent = next((r for r in rows if r["invocation_id"] == child["parent_invocation_id"]), None)
        return parent, _verified(journal, child, parent)


def validate(journal, child):
    """Re-derive the grant from the immutable parent; return that parent."""
    parent, _ = _verify_parent(journal, child)
    return parent


def authorize(journal, command, target_id, *, guard, allow_create=True):
    with journal.read_operation():
        stage = stage_for_attempt(journal, command.run_id, target_id)
        rows = journal.attempts(command.run_id, stage) if stage else ()
        target = next((row for row in rows if row["invocation_id"] == target_id), None)
        if target is None:
            raise RunFailure("local_readmission_not_eligible", run_id=command.run_id)
        child = next(
            (row for row in rows if row["attempt_kind"] == KIND and row["parent_invocation_id"] == target_id),
            None,
        )
        if child is not None:
            _verified(journal, child, target)
            return child
        if not allow_create or rows[-1]["invocation_id"] != target_id:
            raise RunFailure("local_readmission_not_current", run_id=command.run_id, invocation_id=target_id)
        source = _rejected(journal, target)
    guard()
    identity = uuid5(target_id, "local-readmission:1")
    index = target["attempt_index"] + 1
    return journal.prepare(
        command.run_id,
        target["stage"],
        dict(source.request, metadata=_metadata(source.request, identity, index, target_id)),
        source.context,
        attempt_index=index,
        parent_invocation_id=target_id,
        attempt_kind=KIND,
        operation_kind=target["operation_kind"],
        authorization=source.authorization,
    )


def saved_output(journal, invocation):
    """The parent's exact paid response text; this attempt never reaches a provider."""
    if invocation["status"] != "prepared":
        raise RunFailure(
            "local_readmission_action_used",
            run_id=invocation["run_id"],
            invocation_id=invocation["invocation_id"],
        )
    _, source = _verify_parent(journal, invocation)
    return source.text


def admission_policy(journal, invocation, pinned):
    """Return the stamped validator and whether this grant restricts automatic transfers."""
    if invocation["attempt_kind"] != KIND:
        return pinned, False
    _, source = _verify_parent(journal, invocation)
    return source.authorization["admission_contract"], source.authorization["kind"] == AUTHORIZATION
