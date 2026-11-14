"""Atomic private storage boundaries against real PostgreSQL and local object storage."""

from dataclasses import replace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.database import one, query
from podgen.persistence.execution.inputs import bind
from podgen.persistence.private_journal.archive import ExecutionArchiveAdapter
from podgen.persistence.private_journal.contracts import ChargeChange, Checkpoint, HeadChange
from podgen.persistence.private_journal.objects import prepare
from podgen.persistence.private_journal.writer import commit
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile


@pytest.fixture
def private(repo, scope):
    journal = EvaluationJournal(repo, scope[0])
    profile = create_profile(repo, scope[0], model="gpt-6.1-sol", context_limit=100000).ref
    author = journal.record(dict(contract="synthetic-private-core/1"), kind="user_input")
    run = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id="private-core",
        author_ref=author,
        writer_profile_ref=profile,
        reviewer_profile_ref=profile,
    )
    journal.create_run(run, dict(command=command_payload(run)))
    bound = bind(repo, scope[0], run.run_id, [dict(purpose="evaluation", ref=author.model_dump(mode="json"))])
    root = dict(run_id=run.run_id, invocation_id=uuid4())
    command = Checkpoint(
        run_id=root["run_id"],
        attempt_id=root["invocation_id"],
        operation_id=uuid4(),
        kind="attempt_prepared",
        input_set_id=bound["set_id"],
        expected_revision=0,
        data=dict(stage="draft"),
        head=HeadChange(phase="prepared"),
    )
    return repo, scope[0], command


def test_checkpoint_retry_is_exact_and_has_no_generic_projection(private):
    repo, project, command = private
    tables = (
        "runtime_records",
        "runtime_runtime_refs",
        "runtime_blob_refs",
        "evaluation_invocation_events",
        "evaluation_model_invocations",
        "evaluation_model_turns",
        "evaluator_blob_classifications",
    )

    def counts():
        with repo.database.read_transaction() as tx:
            return {
                table: query(tx, f"SELECT count(*) FROM {table} WHERE project_id=:p", p=project).scalar_one()
                for table in tables
            }

    before = counts()
    first = commit(repo, project, command)
    assert commit(repo, project, command) == dict(first, created=False)
    assert counts() == before
    with pytest.raises(Conflict, match="different_content"):
        commit(repo, project, replace(command, data=dict(stage="other")))
    with pytest.raises(Conflict, match="revision_conflict"):
        commit(repo, project, replace(command, operation_id=uuid4()))


def test_archive_full_payload_and_owned_bytes_are_private(private):
    repo, project, command = private
    obj = prepare(b'{"source":"SYNTHETIC complete receipt"}', project)
    ref = RuntimeRef(record_kind="tool_execution", record_id=command.archive_id("receipt"))
    payload = dict(
        contract="synthetic-private/3",
        full="NUL\x00retained",
        value=2**63 - 1,
        body=obj.reference.model_dump(mode="json"),
    )
    command = replace(
        command,
        objects=dict(raw=obj),
        views=(dict(record_id=str(ref.record_id), record_kind=ref.record_kind, payload=payload),),
    )
    commit(repo, project, command)
    adapter = ExecutionArchiveAdapter(repo, project)
    assert adapter.read(ref) == payload
    assert adapter.read_blob(ref, obj.reference) == obj.body
    with pytest.raises(AccessDenied):
        repo.get_runtime(project, ref)
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT count(*) FROM blobs WHERE project_id=:p AND storage_key=:key",
                p=project,
                key=obj.reference.storage_key,
            ).scalar_one()
            == 0
        )


def test_correction_checks_prior_charge_and_preserves_active_step(private):
    repo, project, command = private
    charged = replace(
        command,
        charges=(ChargeChange(command.operation_id, 0, {}, dict(count_calls=1), command.entry_id),),
        head=HeadChange(phase="prepared", accounting_delta=dict(count_calls=1)),
    )
    commit(repo, project, charged)
    correction = replace(
        command,
        kind="accounting_correction",
        operation_id=uuid4(),
        expected_revision=1,
        parents=(command.entry_id,),
        head=HeadChange(preserve_active=True, accounting_delta=dict(count_calls=-1)),
    )
    correction = replace(
        correction,
        charges=(ChargeChange(command.operation_id, 1, dict(count_calls=1), {}, command.entry_id),),
    )
    commit(repo, project, correction)
    assert commit(repo, project, correction)["revision"] == 2
    with repo.database.read_transaction() as tx:
        head = one(
            tx, "SELECT * FROM execution_heads WHERE project_id=:p AND run_id=:r", p=project, r=command.run_id
        )
        assert head["active_attempt_id"] == command.attempt_id and head["phase"] == "prepared"
        assert head["accounting"]["count_calls"] == 0
    with pytest.raises(Conflict, match="charge_revision_conflict"):
        commit(repo, project, replace(correction, operation_id=uuid4(), expected_revision=2))
    with pytest.raises(ContractError, match="cannot_change_coordinator"):
        commit(repo, project, replace(correction, head=replace(correction.head, phase="completed")))


def test_head_explicit_clear_and_unknown_submit_cannot_restart(private):
    repo, project, command = private
    command = replace(
        command, step_id=uuid4(), step_index=0, kind="submit_intent", head=HeadChange(phase="submitting")
    )
    commit(repo, project, command)
    with pytest.raises(Conflict, match="unknown_operation"):
        commit(
            repo,
            project,
            replace(
                command,
                kind="count_intent",
                operation_id=uuid4(),
                expected_revision=1,
                head=HeadChange(phase="counting"),
            ),
        )
