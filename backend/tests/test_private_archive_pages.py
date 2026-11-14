"""Mixed archive pagination preserves identities without unbounded metadata reads."""

from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import json_text, query
from podgen.persistence.private_journal import archive_pages, state
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.test_private_journal_privacy import private_scope as private_scope


def _fact(case, index, revision, *, physical=False, event_views=()):
    row = dict(
        turn_index=index,
        invocation_id=str(uuid5(case.root["invocation_id"], f"source-turn:{index}")),
        root_invocation_id=str(case.root["invocation_id"]),
        request_record_id=str(case.request.record_id),
        status="prepared",
        input_tokens=None,
        output_tokens=None,
        provider_response_id=None,
    )
    data = dict(
        state=dict(
            status="counted" if physical else "response_completed",
            input_tokens=index,
            output_tokens=None if physical else index + 1,
        )
    )
    if physical and index >= 64:
        data["physical"] = row
    return dict(
        project_id=case.project,
        run_id=case.run_id,
        entry_id=uuid4(),
        revision=revision,
        commit_key=str(uuid4()),
        content_hash="synthetic",
        checkpoint_kind="observation",
        operation_id=uuid4(),
        attempt_id=case.root["invocation_id"],
        step_id=uuid5(case.root["invocation_id"], f"source-turn:{index}"),
        step_index=index,
        input_set_id=case.bound["set_id"],
        data=json_text(data),
        archive_views=json_text(list(event_views)),
        archive_payloads=json_text(
            [dict(full="SYNTHETIC full payload must not be selected") for _ in event_views]
        ),
        archive_ids=[UUID(view["record_id"]) for view in event_views],
    )


@pytest.fixture
def mixed_archive(private_scope):
    case = private_scope
    case.root = dict(run_id=case.run_id, invocation_id=uuid4())
    case.request = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    case.repo.append_runtime(case.project, case.request, dict(synthetic=True), audience="evaluator")
    with case.repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_model_invocations
            (project_id,run_id,invocation_id,stage,status,request_record_id)
            VALUES(:p,:r,:a,'draft','prepared',:request)""",
            p=case.project,
            r=case.run_id,
            a=case.root["invocation_id"],
            request=case.request.record_id,
        )
        insert_rows(
            tx,
            "evaluation_model_turns",
            [
                dict(
                    project_id=case.project,
                    root_invocation_id=case.root["invocation_id"],
                    turn_index=index,
                    invocation_id=uuid5(case.root["invocation_id"], f"source-turn:{index}"),
                    status="prepared",
                    request_record_id=case.request.record_id,
                )
                for index in range(64)
            ],
        )
        old_records, old_events = [], []
        for index in range(65):
            identity = uuid4()
            old_records.append(
                dict(
                    project_id=case.project,
                    record_id=identity,
                    record_kind="tool_execution",
                    payload=json_text(
                        dict(
                            contract="model-invocation-event-v1",
                            event_kind="legacy_probe",
                            invocation_id=str(case.root["invocation_id"]),
                            request_ref=case.request.model_dump(mode="json"),
                            data=dict(
                                source_turn_index=index % 64,
                                source_turn_id=str(
                                    uuid5(case.root["invocation_id"], f"source-turn:{index % 64}")
                                ),
                                turn_request_ref=case.request.model_dump(mode="json"),
                            ),
                        )
                    ),
                    audience="evaluator",
                )
            )
            old_events.append(
                dict(
                    project_id=case.project,
                    invocation_id=case.root["invocation_id"],
                    event_id=uuid4(),
                    record_id=identity,
                    event_kind="legacy_probe",
                    sequence=1000 + index,
                    source_turn_index=index % 64,
                )
            )
        insert_rows(tx, "runtime_records", old_records, json_columns=("payload",))
        insert_rows(tx, "evaluation_invocation_events", old_events)
        rows = []
        for index in reversed(range(130)):
            views = [dict(record_id=str(uuid4()), record_kind="tool_execution")]
            views.extend(
                dict(
                    record_id=str(uuid4()),
                    record_kind="tool_execution",
                    event=dict(kind=kind, source_turn_index=index),
                )
                for kind in ("probe", "diagnostic")
            )
            rows.append(_fact(case, index, len(rows) + 1, physical=True, event_views=views))
        for index in range(130):
            rows.append(_fact(case, index, len(rows) + 1))
        # A count candidate without a physical descriptor must not invent a turn.
        ghost = _fact(case, 130, len(rows) + 1)
        ghost["data"] = json_text(dict(state=dict(status="counting")))
        rows.append(ghost)
        for offset in range(0, len(rows), 64):
            insert_rows(
                tx,
                "execution_entries",
                rows[offset : offset + 64],
                json_columns=("data", "archive_views", "archive_payloads"),
            )
    case.journal = EvaluationJournal(case.repo, case.project)
    return case


def test_keyset_pages_match_complete_legacy_private_projection_without_payload_or_per_turn_queries(
    mixed_archive,
):
    case = mixed_archive
    expected_events = state.events(case.journal, case.root)
    expected_turns = state.turns(case.journal, case.root)
    statements = []

    def capture(conn, cursor, sql, params, context, many):
        statements.append(sql)

    event.listen(case.repo.database.engine, "before_cursor_execute", capture)
    try:
        events = list(archive_pages.event_pages(case.journal, case.root))
        turns = list(archive_pages.turn_pages(case.journal, case.root))
    finally:
        event.remove(case.repo.database.engine, "before_cursor_execute", capture)
    assert all(0 < len(page) <= 64 for page in [*events, *turns])
    assert [row for page in events for row in page] == expected_events
    assert [row for page in turns for row in page] == expected_turns
    assert len(expected_turns) == 130
    assert not any(
        "archive_payloads" in sql or "SELECT * FROM execution_entries" in sql for sql in statements
    )
    folds = [sql for sql in statements if "data->'physical'" in sql]
    assert 3 <= len(folds) <= 8  # Packet folds, rather than one query for each of 130 turns.
    assert all("LIMIT" in sql and "step_index=ANY" in sql for sql in folds)
    assert case.repo._read_authorization.get() is None


def test_event_prefix_is_frozen_before_later_legacy_and_private_appends(mixed_archive):
    case = mixed_archive
    expected = state.events(case.journal, case.root)
    pages = archive_pages.event_pages(case.journal, case.root)
    first = next(pages)
    assert case.repo._read_authorization.get() is None
    view = dict(record_id=str(uuid4()), record_kind="tool_execution", event=dict(kind="too_late"))
    late = _fact(case, 0, 262, event_views=(view,))
    with case.repo.database.transaction() as tx:
        insert_rows(
            tx, "execution_entries", [late], json_columns=("data", "archive_views", "archive_payloads")
        )
        query(
            tx,
            """INSERT INTO evaluation_invocation_events
            (project_id,invocation_id,event_id,record_id,event_kind,sequence)
            VALUES(:p,:a,:event,:record,'too_late',999999)""",
            p=case.project,
            a=case.root["invocation_id"],
            event=uuid4(),
            record=case.request.record_id,
        )
    assert [*first, *(row for page in pages for row in page)] == expected
