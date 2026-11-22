"""Address one captured unavailable-source operation; never acquire or invoke a model."""

import hashlib
from dataclasses import dataclass
from uuid import uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.private_journal.state import facts, turn
from podgen.research.hosted.corpus_acquisition import document_url
from podgen.research.hosted.journal import ResearchJournal
from podgen.runtime.models.tool_gateway import _calls

from .source_tool_contract import DURABLE_VERSIONS, from_manifest
from .validation import load_unique_json

VERSION = "local-source-result-recovery/1"
FAILURE = "unbound_private_checkpoint_source"


def require(condition):
    if not condition:
        raise ContractError("source_result_recovery_evidence_mismatch")


@dataclass(frozen=True)
class Evidence:
    root: dict
    turn: dict
    call: dict
    response: dict
    original: dict
    request: dict
    result: dict
    request_ref: RuntimeRef
    result_ref: RuntimeRef


def step_events(journal, root, step_id):
    references = [
        RuntimeRef(record_kind=view["record_kind"], record_id=view["record_id"])
        for fact in facts(
            journal.repo, journal.project, root["run_id"], attempt_id=root["invocation_id"], step_id=step_id
        )
        for view in fact["archive_views"]
        if "event" in view
    ]
    records = []
    with journal.read_operation():
        for start in range(0, len(references), 64):
            batch = references[start : start + 64]
            records.extend(
                dict(value, _record_id=ref.record_id)
                for ref, value in zip(batch, journal.read_many(batch), strict=True)
            )
    return records


def load(journal, run_id, stage, invocation_id, call_id):
    root = journal.invocation(run_id, stage)
    require(
        root is not None
        and root["invocation_id"] == invocation_id
        and root.get("output_record_id") is None
        and root["operation_kind"] == "role"
    )
    manifest = journal.manifest(run_id)
    contract = from_manifest(manifest)
    require(
        journal.run(run_id)["execution_storage"] == "private/3"
        and contract is not None
        and contract["version"] in DURABLE_VERSIONS
    )
    head = journal.execution.current_head(run_id)
    require(head is not None and head["active_attempt_id"] == invocation_id)
    physical = turn(journal, root, head["step_index"])
    require(physical is not None and physical["invocation_id"] == head["active_step_id"])
    events = step_events(journal, root, physical["invocation_id"])
    require(
        root["status"] == physical["status"] == "validation_failed"
        and any(r["event_kind"] == "failure" and r["data"].get("code") == FAILURE for r in events)
        and not any(r["event_kind"] == "source_material_result_recovered" for r in events)
    )
    providers = [r for r in events if r["event_kind"] == "provider_response"]
    calls = [r["data"] for r in events if r["event_kind"] == "source_tool_call"]
    require(len(providers) == len(calls) == 1)
    provider, call = providers[0], calls[0]
    require(
        call["call_id"] == call_id
        and call["name"] == "request_source"
        and provider["provider_http_status"] == 200
        and provider["data"]["source_turn_id"] == str(physical["invocation_id"])
        and call["provider_response_ref"]["record_id"] == str(provider["_record_id"])
    )
    response = load_unique_json(
        journal.read_blob(
            RuntimeRef(record_kind="tool_execution", record_id=provider["_record_id"]),
            BlobRef.model_validate(provider["response_blob"]),
        )
    )
    require(response["status"] == "completed" and response["id"] == physical["provider_response_id"])
    original = journal.request(root)
    native_calls = _calls(
        response,
        web_search=any(t.get("type") == "web_search" for t in original.get("tools", [])),
    )
    require(
        len(native_calls) == 1
        and native_calls[0]["call_id"] == call_id
        and native_calls[0]["name"] == call["name"]
        and native_calls[0]["arguments"] == call["raw_arguments"]
        and hashlib.sha256(call["raw_arguments"].encode()).hexdigest() == call["arguments_hash"]
    )
    arguments = load_unique_json(call["raw_arguments"])
    require(
        isinstance(arguments, dict)
        and arguments == call["arguments"]
        and bool(arguments.get("url")) != bool(arguments.get("source_id"))
    )
    store = ResearchJournal(journal.repo, journal.project, run_id)
    key = "material-request:" + str(uuid5(invocation_id, f"material-request:{call['call_index']}"))
    request, result = store.get(key + ":request"), store.get(key + ":result")
    require(
        request
        == dict(
            name="request_source", arguments=arguments, shared_input_ref=manifest["command"]["author_ref"]
        )
    )
    require(
        isinstance(result, dict)
        and result.get("status") == "unavailable"
        and isinstance(result.get("url"), str)
        and isinstance(result.get("representations"), list)
        and isinstance(result.get("diagnostics"), list)
        and isinstance(result.get("attempt_ref"), dict)
    )
    # Availability belongs to the acquisition/resolver operation, not recovery.
    # A 200 access page, extraction failure or timeout is just as unavailable as
    # a 403. The immutable operation result is authoritative for that decision.
    RuntimeRef.model_validate(result["attempt_ref"])
    if arguments.get("url"):
        require(result["url"] == document_url(arguments["url"]))
    else:
        require(result.get("requested_source_id") == arguments["source_id"])
    require(not any(r["event_kind"] == "source_tool_result" for r in events))
    return Evidence(
        root,
        physical,
        call,
        response,
        original,
        journal.request(physical),
        result,
        store.ref(key + ":request"),
        store.ref(key + ":result"),
    )
