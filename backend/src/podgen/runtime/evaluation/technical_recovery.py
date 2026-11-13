"""Addressed technical recovery; immutable failed attempts retain their cost and provenance."""

from copy import deepcopy
from uuid import uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

from .contracts import RunFailure
from .count_failure_evidence import count_failure_evidence
from .source_tool_recovery import evidence_target, resume_authorization, retry_request
from .technical_recovery_policy import KIND, MAX_STAGE_REPLACEMENTS, STATES, VERSION


def _record(journal, row):
    return journal.read(RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"]))


def evidence(journal, root):
    target = evidence_target(journal, root)
    view, row = target if target else (journal, root)
    count_failure = count_failure_evidence(view, row)
    if count_failure is not None:
        return dict(
            count_event_ref=count_failure["count_event_ref"],
            submit_event_ref=None,
            failure_event_refs=[count_failure["failure_event_ref"]],
            physical_invocation_id=str(row["invocation_id"]),
            provider_response_id=None,
        )
    from .schema_refusal import evidence as schema_refusal

    refusal = schema_refusal(view, row)
    if refusal is not None:
        return dict(**refusal, physical_invocation_id=str(row["invocation_id"]), provider_response_id=None)
    if row["status"] not in STATES or row.get("output_record_id") is not None:
        raise RunFailure(
            "technical_recovery_not_eligible", run_id=root["run_id"], invocation_id=root["invocation_id"]
        )
    submits, failures = [], []
    for event in view.events(row):
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = view.read(ref)
        if record["invocation_id"] != str(row["invocation_id"]) or record["request_ref"]["record_id"] != str(
            row["request_record_id"]
        ):
            raise Conflict("technical_recovery_evidence_mismatch")
        kind, data = event["event_kind"], record["data"]
        if kind == "submit_intent":
            if data.get("client_request_id") != str(row["invocation_id"]):
                raise Conflict("technical_recovery_evidence_mismatch")
            submits.append(ref.model_dump(mode="json"))
        if kind == "output_validation_error":
            raise RunFailure(
                "technical_recovery_not_eligible", run_id=root["run_id"], invocation_id=root["invocation_id"]
            )
        if kind == "provider_error" and data.get("operation") == "submit":
            code = data.get("code", "")
            if (
                data.get("ambiguous") is True
                or code in ("provider_transport_error", "provider_http_408")
                or code.startswith("provider_http_5")
            ):
                failures.append(ref.model_dump(mode="json"))
        if kind == "failure" and data.get("code") in (
            "submission_acknowledgement_unknown",
            "response_not_retrievable",
            "provider_malformed_response",
            "provider_response_id_missing",
        ):
            failures.append(ref.model_dump(mode="json"))
    if len(submits) != 1 or (not failures and row["status"] not in ("submitting", "waiting")):
        raise RunFailure(
            "technical_recovery_not_eligible", run_id=root["run_id"], invocation_id=root["invocation_id"]
        )
    return dict(
        submit_event_ref=submits[0],
        failure_event_refs=failures,
        physical_invocation_id=str(row["invocation_id"]),
        provider_response_id=row.get("provider_response_id"),
    )


def validate_retry(journal, child, parent):
    auth = _record(journal, child).get("authorization") or {}
    rows = journal.attempts(child["run_id"], child["stage"])
    expected = dict(
        kind=VERSION,
        actor_id=str(journal.repo.actor_id),
        target_invocation_id=str(parent["invocation_id"]),
        maximum_new_attempts=1,
        maximum_stage_replacements=MAX_STAGE_REPLACEMENTS,
        evidence=evidence(journal, parent),
        **resume_authorization(journal, parent),
    )
    body, original = journal.request(child), retry_request(journal, parent)
    metadata = dict(
        original["metadata"],
        podgen_invocation_id=str(child["invocation_id"]),
        podgen_attempt_index=str(child["attempt_index"]),
        podgen_parent_invocation_id=str(parent["invocation_id"]),
    )
    if "schema_refusal_event_ref" in expected["evidence"]:
        from podgen.runtime.models.messages_tool_policy import KEY
        from podgen.runtime.models.messages_tool_policy import VERSION as TOOL_POLICY

        metadata[KEY] = TOOL_POLICY
    if (
        auth != expected
        or _record(journal, child)["context"] != _record(journal, parent)["context"]
        or sum(row["attempt_kind"] == KIND for row in rows if row["attempt_index"] <= child["attempt_index"])
        > MAX_STAGE_REPLACEMENTS
        or body != dict(original, metadata=metadata)
        or child["invocation_id"] != uuid5(parent["invocation_id"], "technical-recovery:1")
        or child["operation_kind"] != parent["operation_kind"]
    ):
        raise Conflict("technical_recovery_authorization_mismatch")


def authorize_technical_recovery(
    journal, command, target_id, *, gateway, load_profile, guard, allow_create=True
):
    found = [
        (rows, row)
        for stage in journal.manifest(command.run_id)["stages"]
        if (rows := journal.attempts(command.run_id, stage))
        for row in rows
        if row["invocation_id"] == target_id
    ]
    if not found:
        raise RunFailure("technical_recovery_not_eligible", run_id=command.run_id)
    rows, target = found[0]
    child = next(
        (r for r in rows if r["attempt_kind"] == KIND and r["parent_invocation_id"] == target_id), None
    )
    if child:
        validate_retry(journal, child, target)
        return child
    # A previously reconciled invocation may already have been validated or the run completed.
    if target["status"] in ("response_completed", "tool_running", "completed"):
        if any(e["event_kind"] == "technical_reconciliation_completed" for e in journal.events(target)):
            return target
    # A process can stop after the physical response was committed but before its root's
    # aggregate status was updated. Replay that saved turn, not a new generation.
    from . import model_turns
    from .source_tool_contract import applies, from_manifest

    if (
        allow_create
        and rows[-1]["invocation_id"] == target_id
        and target["status"] in ("submitting", "waiting", "outcome_unknown")
        and target["operation_kind"] == "role"
        and applies(target["stage"], from_manifest(journal.manifest(command.run_id)))
    ):
        turns = model_turns.rows(journal, target)
        if turns and all(turn["status"] == "response_completed" for turn in turns):
            guard()
            journal.event(
                target,
                "technical_reconciliation_completed",
                data=dict(policy=VERSION, previous_status=target["status"], origin="archived_physical_turns"),
            )
            return journal.transition(target, "tool_running")
    receipt = evidence(journal, target)
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("technical_recovery_not_current", run_id=command.run_id, invocation_id=target_id)
    guard()
    from podgen.runtime.models.reconciliation import retrieve

    physical = evidence_target(journal, target)
    turn = physical[1] if physical else target
    view = model_turns.TurnJournal(journal, target, turn["turn_index"]) if physical else journal
    physical_gateway = type(gateway)(
        view, gateway.provider_factory, sleep=gateway.sleep, poll_seconds=gateway.poll_seconds
    )
    profile = load_profile(_record(journal, target)["context"]["profile_ref"], target["stage"])
    if retrieve(physical_gateway, turn, profile, guard=guard, full_response=physical is not None):
        journal.event(
            target,
            "technical_reconciliation_completed",
            data=dict(policy=VERSION, previous_status=target["status"]),
        )
        return journal.transition(target, "tool_running" if physical else "response_completed")
    if sum(row["attempt_kind"] == KIND for row in rows) >= MAX_STAGE_REPLACEMENTS:
        raise RunFailure("technical_recovery_limit_exhausted", run_id=command.run_id, invocation_id=target_id)
    original = _record(journal, target)
    body = deepcopy(retry_request(journal, target))
    identity = uuid5(target_id, "technical-recovery:1")
    body["metadata"].update(
        podgen_invocation_id=str(identity),
        podgen_attempt_index=str(target["attempt_index"] + 1),
        podgen_parent_invocation_id=str(target_id),
    )
    if "schema_refusal_event_ref" in receipt:
        from podgen.runtime.models.messages_tool_policy import KEY
        from podgen.runtime.models.messages_tool_policy import VERSION as TOOL_POLICY

        body["metadata"][KEY] = TOOL_POLICY
    return journal.prepare(
        command.run_id,
        target["stage"],
        body,
        deepcopy(original["context"]),
        attempt_index=target["attempt_index"] + 1,
        parent_invocation_id=target_id,
        attempt_kind=KIND,
        operation_kind=target["operation_kind"],
        authorization=dict(
            kind=VERSION,
            actor_id=str(journal.repo.actor_id),
            target_invocation_id=str(target_id),
            maximum_new_attempts=1,
            maximum_stage_replacements=MAX_STAGE_REPLACEMENTS,
            evidence=receipt,
            **resume_authorization(journal, target),
        ),
    )


def validate_stage(journal, invocation):
    rows = journal.attempts(invocation["run_id"], invocation["stage"])
    by_id = {row["invocation_id"]: row for row in rows}
    for row in rows:
        if row["attempt_kind"] == KIND:
            validate_retry(journal, row, by_id[row["parent_invocation_id"]])


def resume_current(journal, command, *, gateway, load_profile, guard):
    """Ordinary resume recovers its first unfinished submitted stage, never another semantic pass."""
    for stage in journal.manifest(command.run_id)["stages"]:
        rows = journal.attempts(command.run_id, stage)
        if not rows:
            continue
        current = rows[-1]
        if current["status"] == "completed":
            continue
        if current["status"] not in STATES:
            return None
        try:
            return authorize_technical_recovery(
                journal,
                command,
                current["invocation_id"],
                gateway=gateway,
                load_profile=load_profile,
                guard=guard,
            )
        except RunFailure as error:
            if error.code in ("technical_recovery_not_eligible", "source_tool_retry_not_eligible"):
                return None
            raise
    return None
