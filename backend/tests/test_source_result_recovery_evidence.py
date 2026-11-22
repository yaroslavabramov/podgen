"""Reject unrelated, missing or changed evidence before any recovery publication."""

import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.runtime.evaluation import source_result_evidence as evidence
from podgen.runtime.models.tool_result_publication import prepare_result


@pytest.fixture
def captured(monkeypatch):
    run, invocation, provider_ref, acquisition = (uuid4() for _ in range(4))
    physical = dict(
        invocation_id=uuid5(invocation, "source-turn:0"),
        turn_index=0,
        status="validation_failed",
        provider_response_id="resp_saved",
    )
    root = dict(
        run_id=run,
        invocation_id=invocation,
        status="validation_failed",
        operation_kind="role",
        output_record_id=None,
    )
    arguments = dict(source_id=None, url="https://example.com/article", reason="Verify the claim")
    raw = json.dumps(arguments)
    call = dict(
        call_id="source_call",
        name="request_source",
        raw_arguments=raw,
        arguments=arguments,
        arguments_hash=hashlib.sha256(raw.encode()).hexdigest(),
        call_index=0,
        turn_index=0,
        provider_response_ref=dict(record_kind="tool_execution", record_id=str(provider_ref)),
    )
    native = dict(
        status="completed",
        id="resp_saved",
        output=[dict(type="function_call", name="request_source", call_id="source_call", arguments=raw)],
    )
    # BlobRef shape is validated, but the fake journal returns its saved native bytes.
    blob = dict(storage_key="local", sha256="0" * 64, byte_length=1, media_type="application/json")
    events = [
        dict(event_kind="failure", data=dict(code=evidence.FAILURE)),
        dict(event_kind="source_tool_call", data=call),
        dict(
            event_kind="provider_response",
            _record_id=provider_ref,
            provider_http_status=200,
            data=dict(source_turn_id=str(physical["invocation_id"])),
            response_blob=blob,
        ),
    ]
    shared = RuntimeRef(record_kind="user_input", record_id=uuid4()).model_dump(mode="json")
    request = dict(name="request_source", arguments=arguments, shared_input_ref=shared)
    result = dict(
        status="unavailable",
        url=arguments["url"],
        attempt_ref=RuntimeRef(record_kind="tool_execution", record_id=uuid4()).model_dump(mode="json"),
        diagnostics=["http_status_403"],
        representations=[
            dict(
                status="unavailable",
                acquisition_record_ref=dict(record_kind="acquisition_record", record_id=str(acquisition)),
            )
        ],
    )
    receipt = dict(contract="source-received-original/1", requested_url=arguments["url"], status_code=403)
    journal = SimpleNamespace(
        project=uuid4(),
        repo=SimpleNamespace(get_runtime=lambda *a: receipt),
        invocation=lambda *a: root,
        manifest=lambda *a: dict(command=dict(author_ref=shared)),
        run=lambda *a: dict(execution_storage="private/3"),
        execution=SimpleNamespace(
            current_head=lambda *a: dict(
                active_attempt_id=invocation, active_step_id=physical["invocation_id"], step_index=0
            )
        ),
        request=lambda *a: dict(tools=[], input=[]),
        read_blob=lambda *a: json.dumps(native).encode(),
    )
    monkeypatch.setattr(evidence, "from_manifest", lambda *a: dict(version="frozen-corpus-tools/8"))
    monkeypatch.setattr(evidence, "turn", lambda *a: physical)
    monkeypatch.setattr(evidence, "step_events", lambda *a: events)
    key = "material-request:" + str(uuid5(invocation, "material-request:0"))
    records = {key + ":request": request, key + ":result": result}
    monkeypatch.setattr(evidence.ResearchJournal, "get", lambda self, key: records.get(key))
    return SimpleNamespace(**locals())


def test_exact_saved_unavailable_result_is_eligible(captured):
    c = captured
    result = evidence.load(c.journal, c.run, "fact-review", c.invocation, "source_call")
    assert result.result == c.result and result.response == c.native


@pytest.mark.parametrize(
    "damage",
    [
        "call",
        "response",
        "arguments",
        "hash",
        "shared",
        "url",
        "status",
        "incomplete",
        "foreign_operation",
        "pending",
        "failure",
        "uncertain",
    ],
)
def test_changed_or_unrelated_evidence_is_rejected(captured, damage):
    c = captured
    if damage == "call":
        c.call["call_id"] = "foreign_call"
    elif damage == "response":
        c.native["id"] = "foreign_response"
    elif damage == "arguments":
        c.native["output"][0]["arguments"] = "{}"
    elif damage == "hash":
        c.call["arguments_hash"] = "changed"
    elif damage == "shared":
        c.request["shared_input_ref"] = {}
    elif damage == "url":
        c.result["url"] = "https://example.com/other"
    elif damage == "status":
        c.result["status"] = "resolved"
    elif damage == "incomplete":
        c.result.pop("attempt_ref")
    elif damage == "foreign_operation":
        c.call["call_index"] = 1
    elif damage == "pending":
        c.result["status"] = "pending"
    elif damage == "failure":
        c.events[0]["data"]["code"] = "another_failure"
    else:
        c.physical["status"] = "outcome_unknown"
    with pytest.raises(ContractError, match="source_result_recovery_evidence_mismatch"):
        evidence.load(c.journal, c.run, "fact-review", c.invocation, "source_call")


@pytest.mark.parametrize("unavailable", ["access_page_200", "extraction", "timeout", "source_id"])
def test_unavailable_operation_result_is_authoritative(captured, unavailable):
    c = captured
    c.receipt["status_code"] = 200
    c.result["diagnostics"] = [unavailable]
    c.result["representations"] = [] if unavailable == "timeout" else [dict(status="unconfirmed")]
    if unavailable == "source_id":
        c.arguments.update(url=None, source_id="S3")
        raw = json.dumps(c.arguments)
        c.call.update(raw_arguments=raw, arguments_hash=hashlib.sha256(raw.encode()).hexdigest())
        c.native["output"][0]["arguments"] = raw
        c.result["requested_source_id"] = "S3"
    assert evidence.load(c.journal, c.run, "fact-review", c.invocation, "source_call").result == c.result


def test_material_reference_authorization_precedes_publication_and_denials_remain_fatal():
    reference = RuntimeRef(record_kind="acquisition_record", record_id=uuid4())
    root = dict(run_id=uuid4())
    observed = []
    phase = SimpleNamespace(event=lambda *a, **k: observed.append(a[0]))
    journal = SimpleNamespace(
        execution=SimpleNamespace(prepare=lambda *a, **k: observed.append(k)), publication=lambda *a: phase
    )
    result = dict(status="unavailable", attempt_ref=reference.model_dump(mode="json"))
    prepare_result(journal, root, dict(name="request_source"), result)
    assert observed == [dict(evaluation=[reference]), "source_tool_result"]

    def denied(*a, **k):
        raise AccessDenied()

    journal.execution.prepare = denied
    with pytest.raises(AccessDenied):
        prepare_result(journal, root, dict(name="request_source"), result)
    assert observed == [dict(evaluation=[reference]), "source_tool_result"]
