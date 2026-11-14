"""A role result and its provenance complete together; its archive stays private."""

from dataclasses import replace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import walk_values
from podgen.persistence.database import one, query
from podgen.persistence.execution.inputs import bind
from podgen.persistence.execution.live import authorize
from podgen.persistence.private_journal import semantic
from podgen.persistence.private_journal.contracts import HeadChange, SemanticResult
from podgen.persistence.private_journal.objects import prepare
from podgen.persistence.private_journal.writer import commit
from podgen.persistence.runtime_graph import runtime_graph
from tests.test_private_journal_core import private as _private_fixture

private = _private_fixture


def setup_result(case):
    repo, project, prepared = case
    obj = prepare(b'{"request":"SYNTHETIC full request"}', project)
    request = RuntimeRef(record_kind="tool_execution", record_id=prepared.archive_id("request"))
    prepared = replace(
        prepared,
        objects=dict(request=obj),
        views=(
            dict(
                record_id=str(request.record_id),
                record_kind=request.record_kind,
                payload=dict(request_blob=obj.reference.model_dump(mode="json")),
            ),
        ),
    )
    commit(repo, project, prepared)
    result = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    payload = dict(output="SYNTHETIC complete story", request_ref=request.model_dump(mode="json"))
    completed = replace(
        prepared,
        kind="attempt_output",
        operation_id=uuid4(),
        expected_revision=1,
        data=dict(stage="draft"),
        objects={},
        views=(),
        parents=(prepared.entry_id,),
        head=HeadChange(phase="completed"),
    )
    completed = replace(
        completed,
        semantic_result=SemanticResult(
            result,
            payload,
            (prepared.entry_id, completed.entry_id),
        ),
    )
    return repo, project, prepared, completed, result, obj


def test_semantic_result_preserves_archive_view_without_runtime_stub_or_byte_grant(private):
    repo, project, prepared, completed, result, obj = setup_result(private)
    completed.semantic_result.payload["request_ref"]["record_id"] = str(
        prepared.archive_id("request")
    ).upper()
    commit(repo, project, completed)
    stored = repo.get_runtime(project, result)
    assert semantic.restore_locators(stored) == completed.semantic_result.payload
    assert not list(walk_values(stored))
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT count(*) FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=project,
                r=prepared.archive_id("request"),
            ).scalar_one()
            == 0
        )
        assert (
            query(
                tx,
                "SELECT phase FROM execution_heads WHERE project_id=:p AND run_id=:r",
                p=project,
                r=prepared.run_id,
            ).scalar_one()
            == "completed"
        )
    with pytest.raises(AccessDenied):
        repo.read_runtime_blob(project, result, obj.reference)


def test_semantic_link_failure_rolls_back_result_completion_and_entry(private, monkeypatch):
    repo, project, prepared, completed, result, _ = setup_result(private)
    insert = semantic.insert_rows

    def fail_link(tx, table, rows, **kwargs):
        if table == "semantic_execution_links":
            raise RuntimeError("SYNTHETIC semantic link publication failure")
        return insert(tx, table, rows, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(semantic, "insert_rows", fail_link)
        with pytest.raises(RuntimeError, match="link publication failure"):
            commit(repo, project, completed)
    with repo.database.read_transaction() as tx:
        head = one(
            tx,
            "SELECT revision,phase FROM execution_heads WHERE project_id=:p AND run_id=:r",
            p=project,
            r=prepared.run_id,
        )
        assert dict(head) == dict(revision=1, phase="prepared")
        assert (
            query(
                tx,
                "SELECT count(*) FROM execution_entries WHERE project_id=:p AND entry_id=:e",
                p=project,
                e=completed.entry_id,
            ).scalar_one()
            == 0
        )
        assert (
            query(
                tx,
                "SELECT count(*) FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=project,
                r=result.record_id,
            ).scalar_one()
            == 0
        )
    commit(repo, project, completed)
    assert semantic.restore_locators(repo.get_runtime(project, result)) == completed.semantic_result.payload


def test_semantic_denial_is_fresh_with_warm_graph_and_flat_input_generation(private):
    repo, project, prepared, completed, result, _ = setup_result(private)
    commit(repo, project, completed)
    bound = bind(
        repo, project, prepared.run_id, [dict(purpose="evaluation", ref=result.model_dump(mode="json"))]
    )
    with runtime_graph(repo):
        assert repo.get_runtime(project, result)["output"] == completed.semantic_result.payload["output"]
        with repo.database.transaction() as tx:
            query(
                tx,
                "INSERT INTO execution_entry_denials VALUES(:p,:e,:event,'SYNTHETIC denial')",
                p=project,
                e=prepared.entry_id,
                event=uuid4(),
            )
        with pytest.raises(AccessDenied):
            repo.get_runtime(project, result)
        with pytest.raises(AccessDenied), repo.database.read_transaction() as tx:
            authorize(repo, tx, project, bound["set_id"])


def test_semantic_private_reference_requires_declared_provenance(private):
    repo, project, _, completed, _, _ = setup_result(private)
    invalid = replace(
        completed,
        semantic_result=replace(
            completed.semantic_result,
            source_entries=(completed.entry_id,),
        ),
    )
    with pytest.raises(ContractError, match="reference_not_bound"):
        commit(repo, project, invalid)


def test_semantic_result_cannot_shadow_a_private_archive_alias(private):
    repo, project, prepared, completed, _, _ = setup_result(private)
    collision = RuntimeRef(record_kind="tool_execution", record_id=prepared.archive_id("request"))
    invalid = replace(completed, semantic_result=replace(completed.semantic_result, reference=collision))
    with pytest.raises(Conflict, match="identity_is_private_alias"):
        commit(repo, project, invalid)
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT count(*) FROM runtime_records WHERE project_id=:p AND record_id=:r",
                p=project,
                r=collision.record_id,
            ).scalar_one()
            == 0
        )
