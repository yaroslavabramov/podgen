"""Stage-addressed cold state preserves lineage, usage and fresh selected privacy."""

from uuid import UUID, uuid4, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import json_text, query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.execution.inputs import bind
from podgen.persistence.private_journal import attempt_projection
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.usage import attempt_usage
from tests.test_private_journal_privacy import private_scope as private_scope


def _header(case, stage, index=0, parent=None):
    return dict(
        project_id=case.project,
        run_id=case.run_id,
        invocation_id=uuid4(),
        stage=stage,
        status="prepared",
        request_record_id=case.request.record_id,
        attempt_index=index,
        parent_invocation_id=parent,
        parent_attempt_index=0 if parent else None,
        attempt_kind="input_budget_retry" if parent else "role",
        operation_kind="role",
        provider_response_id=None,
        input_tokens=None,
        output_tokens=None,
        output_record_id=None,
    )


def _fact(case, attempt, data, *, step=None, bound=None):
    case.revision += 1
    return dict(
        project_id=case.project,
        run_id=case.run_id,
        entry_id=uuid4(),
        revision=case.revision,
        commit_key=str(uuid4()),
        content_hash="synthetic",
        checkpoint_kind="observation",
        operation_id=uuid4(),
        attempt_id=attempt,
        step_index=step,
        step_id=uuid5(attempt, f"step:{step}") if step is not None else None,
        input_set_id=(bound or case.bound)["set_id"],
        data=json_text(data),
        archive_views=json_text([dict(record_id=str(uuid4()), record_kind="tool_execution")]),
        archive_payloads=json_text([dict(full="SYNTHETIC complete history must not enter cold state")]),
    )


def _wire(row):
    return {key: str(value) if isinstance(value, UUID) else value for key, value in row.items()}


@pytest.fixture
def attempts_case(private_scope):
    case = private_scope
    case.request = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    case.repo.append_runtime(case.project, case.request, dict(synthetic=True), audience="evaluator")
    old = _header(case, "draft")
    old.update(status="validation_failed", input_tokens=7)
    selected = _header(case, "draft", 1, old["invocation_id"])
    irrelevant = _header(case, "review")
    late_source = case.repo.create(
        case.project,
        "Policy",
        dict(policy_kind="data", rules=dict(late="source")),
        producer=case.producer,
        operation_id=uuid4(),
    )
    late = bind(
        case.repo,
        case.project,
        case.run_id,
        [dict(purpose="evaluation", ref=late_source.ref.model_dump(mode="json"))],
    )
    rows = [
        _fact(case, selected["invocation_id"], dict(attempt=_wire(selected))),
        _fact(case, irrelevant["invocation_id"], dict(attempt=_wire(irrelevant)), bound=late),
        _fact(case, old["invocation_id"], dict(state=dict(input_tokens=9))),
        _fact(case, selected["invocation_id"], dict(state=dict(status="counted", input_tokens=11))),
        _fact(case, selected["invocation_id"], dict(state=dict(status="failed", input_tokens=999)), step=0),
        _fact(case, selected["invocation_id"], {}, step=0),
        _fact(
            case,
            selected["invocation_id"],
            dict(
                state=dict(
                    status="response_completed",
                    input_tokens=17,
                    output_tokens=5,
                    provider_response_id="resp_synthetic",
                )
            ),
        ),
    ]
    output = uuid4()
    rows.append(
        _fact(
            case,
            selected["invocation_id"],
            dict(state=dict(status="completed", output_record_id=str(output))),
        )
    )
    for index in range(130):
        rows.append(
            _fact(
                case,
                selected["invocation_id"],
                dict(state=dict(status="outcome_unknown", input_tokens=999)),
                step=index,
            )
        )
        rows.append(
            _fact(
                case,
                irrelevant["invocation_id"],
                dict(state=dict(status="failed"), irrelevant="SYNTHETIC history"),
                step=index,
                bound=late,
            )
        )
    with case.repo.database.transaction() as tx:
        insert_rows(tx, "evaluation_model_invocations", [old])
        for offset in range(0, len(rows), 64):
            insert_rows(
                tx,
                "execution_entries",
                rows[offset : offset + 64],
                json_columns=("data", "archive_views", "archive_payloads"),
            )
    case.journal = EvaluationJournal(case.repo, case.project)
    case.old, case.selected, case.irrelevant = old, selected, irrelevant
    case.late_source, case.output = late_source, output
    case.selected_tail = next(
        row["entry_id"] for row in reversed(rows) if row["attempt_id"] == selected["invocation_id"]
    )
    return case


def test_selected_stage_keeps_mixed_legacy_private_status_usage_and_reads_compact_pages(
    attempts_case, monkeypatch
):
    case = attempts_case
    calls, original = [], attempt_projection.query
    selected = {case.old["invocation_id"], case.selected["invocation_id"]}

    def capture(tx, sql, **params):
        calls.append((sql, params))
        if "data->'state' AS state" in sql:
            assert set(params["ids"]) <= selected
        if "data->'attempt' AS attempt" in sql:
            assert params["stage"] == "draft"
        return original(tx, sql, **params)

    monkeypatch.setattr(attempt_projection, "query", capture)
    rows = case.journal.attempts(case.run_id, "draft")
    expected_old = dict(case.old, input_tokens=9)
    expected = dict(
        case.selected,
        status="completed",
        input_tokens=17,
        output_tokens=5,
        provider_response_id="resp_synthetic",
        output_record_id=case.output,
    )
    assert rows == [expected_old, expected]
    folds = [sql for sql, _ in calls if "data->'state' AS state" in sql]
    assert 2 <= len(folds) <= 4
    assert all("LIMIT :batch" in sql for sql in folds)
    assert not any("archive_views" in sql or "archive_payloads" in sql for sql, _ in calls)
    assert case.repo._read_authorization.get() is None
    assert attempt_usage(case.journal, rows[-1], 1000) == dict(input=17, output=5)


def test_other_stage_generation_withdrawal_does_not_block_selected_or_absent_stage(attempts_case):
    case = attempts_case
    assert case.journal.attempts(case.run_id, "review")
    DependencyLedger(case.repo).invalidate(
        case.project, case.late_source.ref, reason="SYNTHETIC later stage withdrawal", category="access"
    )
    assert case.journal.invocation(case.run_id, "draft")["output_record_id"] == case.output
    assert case.journal.attempts(case.run_id, "plan") == []
    with pytest.raises(AccessDenied):
        case.journal.attempts(case.run_id, "review")


@pytest.mark.parametrize("denial", ("entry", "membership"))
def test_selected_entry_or_membership_denial_is_fresh_after_warm_projection(attempts_case, denial):
    case = attempts_case
    assert case.journal.invocation(case.run_id, "draft")["status"] == "completed"
    with case.repo.database.transaction() as tx:
        if denial == "entry":
            query(
                tx,
                "INSERT INTO execution_entry_denials VALUES(:p,:entry,:event,'SYNTHETIC selected denial')",
                p=case.project,
                entry=case.selected_tail,
                event=uuid4(),
            )
        else:
            query(
                tx,
                "UPDATE project_members SET role='editor' WHERE project_id=:p AND actor_id=:actor",
                p=case.project,
                actor=case.repo.actor_id,
            )
    with pytest.raises(AccessDenied):
        case.journal.invocation(case.run_id, "draft")
