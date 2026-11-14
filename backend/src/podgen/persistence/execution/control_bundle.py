"""One bounded, narrow metadata bundle for the checkpoint's unique control IDs."""

from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import json_text, query

OUTPUTS = {"writing-evaluation-output-v1", "independent-reading-output/1"}


def _error():
    raise ContractError("execution_control_binding_mismatch")


def load(tx, project, run_id, set_id, generation, records, pending):
    parents, root_rows, turn_rows, calls = {}, {}, {}, {}
    wanted, invocations, pairs, call_pairs = {}, set(), set(), set()
    for _, payload, _ in records:
        contract = payload.get("contract")
        paths = []
        if contract == "model-invocation-event-v1" or contract in OUTPUTS:
            try:
                invocation = UUID(payload["invocation_id"])
            except (ValueError, KeyError, TypeError):
                _error()
            invocations.add(invocation)
            paths.append(payload.get("request_ref"))
            data = payload.get("data", {})
            if contract == "model-invocation-event-v1":
                paths.extend(
                    data[name]
                    for name in (
                        "turn_request_ref",
                        "count_ref",
                        "count_intent_ref",
                        "checkpoint_result_ref",
                        "provider_response_ref",
                    )
                    if name in data
                )
                if payload.get("event_kind") in ("source_tool_call", "source_tool_result") and data.get(
                    "provider_response_ref"
                ):
                    call_pairs.add((str(data["provider_response_ref"].get("record_id")), data.get("call_id")))
                if "turn_request_ref" in data:
                    index = data.get("source_turn_index")
                    if type(index) is not int or index < 0:
                        _error()
                    pairs.add((invocation, index))
        elif contract == "model-request-v1" and payload.get("parent_request_ref"):
            paths.append(payload["parent_request_ref"])
        for value in paths:
            try:
                ref = RuntimeRef.model_validate(value)
            except (ValueError, TypeError):
                _error()
            if ref.record_kind != "tool_execution":
                _error()
            if ref.record_id not in pending:
                wanted[ref.record_id] = ref
    if wanted:
        rows = query(
            tx,
            """SELECT rr.record_id,rr.record_kind,rr.execution_input_set_id,s.generation,
          jsonb_build_object('contract',rr.payload->'contract','invocation_id',rr.payload->'invocation_id',
             'request_ref',rr.payload->'request_ref','event_kind',rr.payload->'event_kind',
             'context',jsonb_build_object('source_tool_turn',rr.payload->'context'->'source_tool_turn'),
             'data',jsonb_build_object('native_request_blob',rr.payload->'data'->'native_request_blob',
                'count_identity',rr.payload->'data'->'count_identity','input_tokens',rr.payload->'data'->'input_tokens',
                'source_turn_index',rr.payload->'data'->'source_turn_index','source_turn_id',rr.payload->'data'->'source_turn_id',
                'name',rr.payload->'data'->'name','call_index',rr.payload->'data'->'call_index',
                'turn_request_ref',rr.payload->'data'->'turn_request_ref')) AS payload,
          rr.execution_run_id=:r OR EXISTS(SELECT 1 FROM evaluation_invocation_events e
             JOIN evaluation_model_invocations i USING(project_id,invocation_id)
             WHERE e.project_id=rr.project_id AND e.record_id=rr.record_id AND i.run_id=:r)
          OR EXISTS(SELECT 1 FROM evaluation_model_invocations i WHERE i.project_id=rr.project_id
             AND i.run_id=:r AND i.request_record_id=rr.record_id)
          OR EXISTS(SELECT 1 FROM evaluation_model_turns t JOIN evaluation_model_invocations i
             ON i.project_id=t.project_id AND i.invocation_id=t.root_invocation_id
             WHERE t.project_id=rr.project_id AND i.run_id=:r AND t.request_record_id=rr.record_id) AS owned,
          EXISTS(SELECT 1 FROM execution_input_resources z WHERE z.project_id=:p AND z.run_id=:r AND z.set_id=:s
             AND z.purpose='evaluation' AND z.resource_kind='runtime' AND z.owner_project_id=:p
             AND z.identity=rr.record_id::text AND z.read_required
             AND z.binding->>'record_kind'=rr.record_kind) AS covered
          FROM runtime_records rr LEFT JOIN execution_input_sets s ON s.project_id=rr.project_id
            AND s.run_id=rr.execution_run_id AND s.set_id=rr.execution_input_set_id
          WHERE rr.project_id=:p AND rr.record_id=ANY(CAST(:ids AS uuid[]))""",
            p=project,
            r=run_id,
            s=set_id,
            ids=list(wanted),
        ).mappings()
        parents = {row["record_id"]: row for row in rows}
        for identity, ref in wanted.items():
            row = parents.get(identity)
            if (
                row is None
                or not row["owned"]
                or row["record_kind"] != ref.record_kind
                or (row["execution_input_set_id"] is not None and row["generation"] > generation)
                or (row["execution_input_set_id"] is None and not row["covered"])
            ):
                _error()
    if invocations:
        root_rows = {
            row["invocation_id"]: row
            for row in query(
                tx,
                """SELECT invocation_id,request_record_id
            FROM evaluation_model_invocations WHERE project_id=:p AND run_id=:r
             AND invocation_id=ANY(CAST(:ids AS uuid[]))""",
                p=project,
                r=run_id,
                ids=list(invocations),
            ).mappings()
        }
    if pairs:
        turn_rows = {
            (row["root_invocation_id"], row["turn_index"]): row
            for row in query(
                tx,
                """SELECT t.root_invocation_id,
            t.turn_index,t.invocation_id,t.request_record_id FROM evaluation_model_turns t
            JOIN jsonb_to_recordset(CAST(:pairs AS jsonb)) AS wanted(root text,n integer)
              ON t.root_invocation_id=wanted.root::uuid AND t.turn_index=wanted.n
            WHERE t.project_id=:p""",
                p=project,
                pairs=json_text([dict(root=str(root), n=n) for root, n in pairs]),
            ).mappings()
        }

    if call_pairs:
        calls = {
            (row["response_record_id"], row["call_id"]): row
            for row in query(
                tx,
                """SELECT c.* FROM execution_provider_calls c
                JOIN jsonb_to_recordset(CAST(:pairs AS jsonb)) AS wanted(response uuid, call text)
                  ON c.response_record_id=wanted.response AND c.call_id=wanted.call
                WHERE c.project_id=:p AND c.run_id=:r""",
                p=project,
                r=run_id,
                pairs=json_text([dict(response=response, call=call) for response, call in call_pairs]),
            ).mappings()
        }
    return parents, root_rows, turn_rows, calls
