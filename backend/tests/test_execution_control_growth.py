"""Measure actual parent lookup plans with growing unrelated persisted history."""

import json
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import json_text, query
from podgen.persistence.execution import control_bundle
from podgen.persistence.execution.inputs import bind
from tests.test_model_step_publication import setup


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


@pytest.mark.parametrize("history_size", [0, 128, 4096])
def test_parent_bundle_uses_bounded_queries_and_indexes_for_large_history(
    repo, scope, monkeypatch, history_size
):
    journal, root, *_ = setup(repo, scope)
    root_ref = RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"])
    intent = journal.record(
        dict(
            contract="model-invocation-event-v1",
            invocation_id=str(root["invocation_id"]),
            event_kind="source_context_count_intent",
            request_ref=root_ref.model_dump(mode="json"),
            data={},
        )
    )
    with repo.database.transaction() as tx:
        identities = [intent.record_id] + [uuid4() for _ in range(history_size)]
        for offset in range(1, len(identities), 128):
            insert_rows(
                tx,
                "runtime_records",
                [
                    dict(
                        project_id=scope[0],
                        record_id=identity,
                        record_kind="tool_execution",
                        payload=json_text(
                            dict(contract="model-invocation-event-v1", event_kind="failure", data={})
                        ),
                        audience="evaluator",
                    )
                    for identity in identities[offset : offset + 128]
                ],
                json_columns=("payload",),
            )
        for offset in range(0, len(identities), 128):
            insert_rows(
                tx,
                "evaluation_invocation_events",
                [
                    dict(
                        project_id=scope[0],
                        invocation_id=root["invocation_id"],
                        event_id=uuid4(),
                        record_id=identity,
                        event_kind="source_context_count_intent"
                        if identity == intent.record_id
                        else "failure",
                        source_turn_index=None,
                    )
                    for identity in identities[offset : offset + 128]
                ],
            )
        query(tx, "ANALYZE runtime_records")
        query(tx, "ANALYZE evaluation_invocation_events")
    bound = bind(
        repo, scope[0], root["run_id"], [dict(purpose="evaluation", ref=intent.model_dump(mode="json"))]
    )
    consumer = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    payload = dict(
        contract="model-invocation-event-v1",
        invocation_id=str(root["invocation_id"]),
        event_kind="source_context_count",
        request_ref=root_ref.model_dump(mode="json"),
        data=dict(count_intent_ref=intent.model_dump(mode="json")),
    )
    captured = []

    def measured(tx, sql, **params):
        captured.append((sql, params))
        return query(tx, sql, **params)

    monkeypatch.setattr(control_bundle, "query", measured)
    with repo.database.read_transaction() as tx:
        parents, invocations, turns, calls = control_bundle.load(
            tx,
            scope[0],
            root["run_id"],
            bound["set_id"],
            bound["generation"],
            [(consumer, payload, False)],
            {consumer.record_id: (consumer, payload)},
        )
        assert set(parents) == {root_ref.record_id, intent.record_id}
        assert len(invocations) == 1 and not turns and not calls
        assert len(captured) == 2
        sql, params = captured[0]
        plan = query(tx, "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, **params).scalar()[0]
    visited = [
        dict(
            table=node.get("Relation Name"),
            alias=node.get("Alias"),
            method=node["Node Type"],
            rows=node.get("Actual Rows", 0),
            loops=node.get("Actual Loops", 0),
            removed=node.get("Rows Removed by Filter", 0),
        )
        for node in nodes(plan["Plan"])
        if node.get("Relation Name") in ("runtime_records", "evaluation_invocation_events")
    ]
    scanned = sum((node["rows"] + node["removed"]) * node["loops"] for node in visited)
    # PostgreSQL can prefer a sequential/hashed plan for tiny tables. Report
    # that real work; require indexed exact lookups once history is substantial.
    assert scanned <= (16 if history_size >= 4096 else 300), visited
    print(
        json.dumps(
            dict(
                history_size=history_size,
                parent_rows=len(parents),
                queries=len(captured),
                actual_rows_visited=scanned,
                plans=visited,
            )
        )
    )
