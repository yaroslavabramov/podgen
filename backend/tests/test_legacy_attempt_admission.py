"""First private preparation preserves legacy failed and completed execution archives."""

import json
from copy import deepcopy
from uuid import uuid4, uuid5

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import query
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.technical_recovery import authorize_technical_recovery, validate_retry
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.messages_tool_policy import KEY, VERSION
from podgen.runtime.models.messages_wire import messages_request
from tests.test_messages_grammar_recovery import GRAMMAR_ERROR, request


def archive(repo, project):
    with repo.database.read_transaction() as tx:
        return {
            table: list(
                query(
                    tx,
                    f"SELECT row_to_json(t) FROM {table} t WHERE project_id=:p ORDER BY to_jsonb(t)::text",
                    p=project,
                ).scalars()
            )
            for table in (
                "runtime_records",
                "evaluation_model_invocations",
                "evaluation_model_turns",
                "evaluation_invocation_events",
            )
        }


def legacy(repo, scope, status):
    patch = pytest.MonkeyPatch()
    memory, profile, _, _, body = request(patch)
    patch.undo()
    journal = EvaluationJournal(repo, scope[0])
    profile_ref = create_profile(
        repo, scope[0], model=body["model"], provider="anthropic", context_limit=100000
    ).ref
    author = journal.record(dict(contract="legacy-admission-source/1"), kind="user_input")
    command = WritingRunCommand(
        case_id="legacy-attempt-admission",
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        author_ref=author,
        writer_profile_ref=profile_ref,
        reviewer_profile_ref=profile_ref,
    )
    root_id = uuid5(command.run_id, "invocation:revise-1")
    physical = uuid5(root_id, "source-turn:0")
    body["metadata"].update(
        podgen_run_id=str(command.run_id), podgen_invocation_id=str(root_id), podgen_stage="revise-1"
    )
    manifest = journal.record(
        dict(memory.snapshot, stages=["revise-1", "reader-review-1"], command=command_payload(command)),
        kind="context_assembly",
    )
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_writing_runs(project_id,run_id,actor_id,input_fingerprint,manifest_id,status)
            VALUES(:p,:r,:a,:hash,:manifest,'needs_attention')""",
            p=scope[0],
            r=command.run_id,
            a=repo.actor_id,
            hash=digest(command_payload(command)),
            manifest=manifest.record_id,
        )
    root_ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(root_id, "request"))
    physical_ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(physical, "request"))
    context = dict(profile_ref=profile_ref.model_dump(mode="json"))
    for ref, identity, candidate in (
        (root_ref, root_id, body),
        (
            physical_ref,
            physical,
            dict(
                body,
                metadata=dict(body["metadata"], podgen_invocation_id=str(physical), podgen_source_turn="0"),
            ),
        ),
    ):
        blob = journal.blob(json.dumps(candidate).encode())
        repo.append_runtime(
            scope[0],
            ref,
            dict(
                contract="model-request-v1",
                invocation_id=str(identity),
                run_id=str(command.run_id),
                stage="revise-1",
                context=context,
                request_blob=blob.model_dump(mode="json"),
            ),
            audience="evaluator",
        )
    output = None
    if status == "completed":
        output = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        repo.append_runtime(
            scope[0],
            output,
            dict(contract="writing-evaluation-output-v1", output=dict(utterances=[])),
            audience="evaluator",
        )
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_model_invocations(project_id,run_id,invocation_id,stage,status,request_record_id,input_tokens,output_tokens,output_record_id)
            VALUES(:p,:r,:i,'revise-1',:status,:request,100,:tokens,:output)""",
            p=scope[0],
            r=command.run_id,
            i=root_id,
            status=status,
            request=root_ref.record_id,
            tokens=10 if output else None,
            output=output.record_id if output else None,
        )
        query(
            tx,
            """INSERT INTO evaluation_model_turns(project_id,root_invocation_id,turn_index,invocation_id,status,request_record_id,input_tokens,output_tokens)
            VALUES(:p,:root,0,:i,:status,:request,100,:tokens)""",
            p=scope[0],
            root=root_id,
            i=physical,
            status="response_completed" if output else status,
            request=physical_ref.record_id,
            tokens=10 if output else None,
        )
    if status == "failed":
        physical_body = dict(
            body, metadata=dict(body["metadata"], podgen_invocation_id=str(physical), podgen_source_turn="0")
        )
        for kind, data in (
            ("submit_intent", dict(client_request_id=str(physical))),
            ("provider_error", dict(operation="submit", ambiguous=False, code="provider_http_400")),
        ):
            ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
            payload = dict(
                contract="model-invocation-event-v1",
                invocation_id=str(root_id),
                event_kind=kind,
                request_ref=root_ref.model_dump(mode="json"),
                data=dict(
                    data,
                    source_turn_index=0,
                    source_turn_id=str(physical),
                    turn_request_ref=physical_ref.model_dump(mode="json"),
                ),
            )
            if kind == "provider_error":
                payload.update(
                    provider_http_status=400,
                    response_blob=journal.blob(json.dumps(GRAMMAR_ERROR).encode()).model_dump(mode="json"),
                    wire_request_blob=journal.blob(
                        json.dumps(messages_request(physical_body)).encode()
                    ).model_dump(mode="json"),
                )
            repo.append_runtime(scope[0], ref, payload, audience="evaluator")
            with repo.database.transaction() as tx:
                query(
                    tx,
                    "INSERT INTO evaluation_invocation_events(project_id,invocation_id,event_id,record_id,event_kind,source_turn_index) VALUES(:p,:i,:event,:record,:kind,0)",
                    p=scope[0],
                    i=root_id,
                    event=uuid4(),
                    record=ref.record_id,
                    kind=kind,
                )
    return journal, command, profile, body, context


def test_legacy_failed_revision_schema_refusal_prepares_bound_private_child(repo, scope):
    journal, command, profile, _, _ = legacy(repo, scope, "failed")
    before = archive(repo, scope[0])
    parent = journal.invocation(command.run_id, "revise-1")
    saved = deepcopy(journal.request(parent))

    def forbidden(_):
        pytest.fail("preparing a proven rejected legacy request contacted a provider")

    child = authorize_technical_recovery(
        journal,
        command,
        parent["invocation_id"],
        gateway=ModelGateway(journal, forbidden),
        load_profile=lambda *args: profile,
        guard=lambda: None,
    )
    validate_retry(journal, child, parent)
    assert child["attempt_kind"] == "technical_retry" and child["status"] == "prepared"
    assert journal.request(child)["metadata"][KEY] == VERSION
    assert journal.request(parent) == saved and archive(repo, scope[0]) == before
    assert journal.run(command.run_id)["execution_storage"] == "private/3"


def test_next_stage_after_completed_legacy_attempt_adopts_before_private_prepare(repo, scope):
    journal, command, _, body, context = legacy(repo, scope, "completed")
    before = archive(repo, scope[0])
    child = journal.prepare(command.run_id, "reader-review-1", body, context)
    assert child["status"] == "prepared"
    assert journal.run(command.run_id)["execution_storage"] == "private/3"
    assert archive(repo, scope[0]) == before
    assert journal.invocation(command.run_id, "revise-1")["status"] == "completed"
