"""Address an atomically recovered result without reopening its original bodies."""

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.private_journal.state import facts

from .source_result_evidence import VERSION, require


def existing(journal, run_id, stage, invocation_id, call_id):
    head = journal.execution.current_head(run_id)
    if head is None or head["active_attempt_id"] != invocation_id:
        return None
    matches = [
        (fact, view)
        for fact in facts(
            journal.repo, journal.project, run_id, attempt_id=invocation_id, step_id=head["active_step_id"]
        )
        for view in fact["archive_views"]
        if view.get("event", {}).get("kind") == "source_material_result_recovered"
    ]
    require(len(matches) <= 1)
    if not matches:
        return None
    fact, view = matches[0]
    event = journal.read(RuntimeRef(record_kind=view["record_kind"], record_id=view["record_id"]))
    data = event["data"]
    outcome = data["outcome"]
    require(
        event["invocation_id"] == str(invocation_id)
        and data["policy"] == VERSION
        and data["call_id"] == call_id
        and data["turn_index"] == head["step_index"]
        and head["phase"] in ("tool_running", "response_completed")
        and outcome["run_id"] == str(run_id)
        and outcome["invocation_id"] == str(invocation_id)
        and outcome["call_id"] == call_id
        and outcome["next_turn"] == head["step_index"] + 1
    )
    owned = {item["record_id"] for item in fact["archive_views"]}
    require(
        outcome["next_request_ref"]["record_id"] in owned and outcome["tool_result_ref"]["record_id"] in owned
    )
    # The exact prepared record owns the saved request/result references. Reading
    # it checks their entire admitted closure under current permissions; no byte
    # body or historical decision needs decoding for this idempotent result read.
    prepared = journal.read(RuntimeRef.model_validate(outcome["next_request_ref"]))
    require(
        prepared["contract"] == VERSION
        and prepared["stage"] == stage
        and prepared["next_turn"] == outcome["next_turn"]
        and prepared["request_hash"] == outcome["request_hash"]
        and prepared["tool_result_ref"] == outcome["tool_result_ref"]
    )
    return outcome
