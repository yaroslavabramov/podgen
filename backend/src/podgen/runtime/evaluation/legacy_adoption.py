"""Admit old immutable execution into the new coordinator without rewriting its archive."""

from dataclasses import replace
from uuid import uuid5

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.persistence.database import one, query
from podgen.persistence.private_journal.contracts import ChargeChange, Checkpoint, HeadChange, operation_id
from podgen.persistence.private_journal.head import next_head
from podgen.persistence.private_journal.writer import commit
from podgen.persistence.read_authorization import _read_connection


def before_prepare(journal, run_id, stage, attempt_id, parent):
    """Admit the last archived owner before the first new private stage/attempt."""
    if journal.execution.current_head(run_id) is not None:
        return
    if journal.run(run_id)["execution_storage"] != "legacy":
        return
    root = parent
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        if root is None:
            stages = journal.manifest(run_id)["stages"]
            preceding = stages[: stages.index(stage)]
            saved = one(
                tx,
                """SELECT * FROM evaluation_model_invocations
                WHERE project_id=:p AND run_id=:r AND stage=ANY(CAST(:stages AS text[]))
                ORDER BY array_position(CAST(:stages AS text[]),stage) DESC,attempt_index DESC LIMIT 1""",
                p=journal.project,
                r=run_id,
                stages=preceding,
            )
            root = dict(saved) if saved else None
        turn = (
            one(
                tx,
                """SELECT * FROM evaluation_model_turns
            WHERE project_id=:p AND root_invocation_id=:root
            ORDER BY turn_index DESC LIMIT 1""",
                p=journal.project,
                root=root["invocation_id"],
            )
            if root
            else None
        )
    if root is not None:
        adopt(journal, root, dict(root, **turn) if turn else root)
        return
    # An old run may have stopped after its manifest, before any model attempt.
    bound = journal.execution.prepare(run_id)
    command = Checkpoint(
        run_id=run_id,
        attempt_id=attempt_id,
        operation_id=operation_id(run_id, "legacy_empty"),
        kind="legacy_adopted",
        input_set_id=bound["set_id"],
        expected_revision=0,
        data=dict(legacy_binding=dict(manifest_only=True)),
        head=HeadChange(phase="prepared"),
    )
    commit(journal.repo, journal.project, command)
    journal.execution.committed(run_id, next_head(None, command))


def adopt(journal, root, invocation):
    run_id = root["run_id"]
    references = [
        RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"])
        for row in (root, invocation)
    ]
    bound = journal.execution.prepare(run_id, evaluation=references)
    request = journal.read(references[0])
    profile_ref = request.get("context", {}).get("profile_ref")
    reservation = (
        journal.repo.get(journal.project, ArtifactRef.model_validate(profile_ref)).payload["output_limit"]
        if profile_ref
        else 0
    )
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        events = {
            row["event_kind"]: row["n"]
            for row in query(
                tx,
                """SELECT e.event_kind,count(*) AS n
            FROM evaluation_invocation_events e JOIN evaluation_model_invocations i USING(project_id,invocation_id)
            WHERE i.project_id=:p AND i.run_id=:r GROUP BY e.event_kind""",
                p=journal.project,
                r=run_id,
            ).mappings()
        }
        turns = list(
            query(
                tx,
                """SELECT t.* FROM evaluation_model_turns t JOIN evaluation_model_invocations i
            ON i.project_id=t.project_id AND i.invocation_id=t.root_invocation_id
            WHERE i.project_id=:p AND i.run_id=:r""",
                p=journal.project,
                r=run_id,
            ).mappings()
        )
    charges = dict(
        count_calls=events.get("source_context_count_intent", 0) + events.get("count_intent", 0),
        count_reuses=events.get("count_receipt_reused", 0),
        generation_calls=events.get("submit_intent", 0),
    )
    submitted = [
        row
        for row in turns
        if row["status"] in ("submitting", "waiting", "response_completed", "outcome_unknown")
    ]
    charges.update(
        input_tokens=sum(row["input_tokens"] or 0 for row in submitted),
        output_tokens=sum(row["output_tokens"] or 0 for row in submitted),
        output_reserved=sum(reservation for row in submitted if row["output_tokens"] is None),
        uncertain_calls=sum(row["output_tokens"] is None for row in submitted),
    )
    charges = {key: value for key, value in charges.items() if value}
    status = invocation["status"]
    command = Checkpoint(
        run_id=run_id,
        attempt_id=root["invocation_id"],
        operation_id=operation_id(root["invocation_id"], "legacy_adopt"),
        kind="legacy_adopted",
        input_set_id=bound["set_id"],
        expected_revision=0,
        step_id=invocation["invocation_id"],
        step_index=invocation.get("turn_index"),
        data=dict(
            legacy_binding=dict(
                root_invocation_id=str(root["invocation_id"]),
                invocation_id=str(invocation["invocation_id"]),
                request_record_id=str(invocation["request_record_id"]),
            ),
            state=dict(status=status),
        ),
        head=HeadChange(phase=status, accounting_delta=charges),
    )
    command = replace(
        command,
        head=replace(
            command.head,
            request_entry_id=command.entry_id,
            submit_entry_id=command.entry_id
            if status in ("submitting", "waiting", "outcome_unknown")
            else None,
        ),
        charges=(ChargeChange(uuid5(run_id, "private/3/legacy-charge"), 0, {}, charges, command.entry_id),)
        if charges
        else (),
    )
    commit(journal.repo, journal.project, command)
    journal.execution.committed(run_id, next_head(None, command))
    return journal.execution.current_head(run_id)
