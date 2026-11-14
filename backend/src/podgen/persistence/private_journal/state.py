"""Cold projections of immutable attempt/step facts; the head stays fixed-size."""

from uuid import UUID, uuid5

from podgen.persistence.database import one, query
from podgen.persistence.execution.reads import current_inputs
from podgen.persistence.read_authorization import _read_connection

from . import access

UUID_FIELDS = frozenset(
    {
        "project_id",
        "run_id",
        "invocation_id",
        "root_invocation_id",
        "request_record_id",
        "parent_invocation_id",
        "output_record_id",
    }
)


def identity_row(value):
    return {
        key: UUID(child) if key in UUID_FIELDS and isinstance(child, str) else child
        for key, child in value.items()
    }


@current_inputs
def head(repo, project, run_id):
    with _read_connection(repo) as tx:
        access.run_access(repo, tx, project, run_id)
        row = one(tx, "SELECT * FROM execution_heads WHERE project_id=:p AND run_id=:r", p=project, r=run_id)
        if row:
            access.inputs(repo, tx, project, run_id, row["input_set_id"])
            access.entry_privacy(tx, project, [row["last_entry_id"]])
        return dict(row) if row else None


@current_inputs
def facts(repo, project, run_id, *, attempt_id=None, step_id=None):
    with _read_connection(repo) as tx:
        access.run_access(repo, tx, project, run_id)
        restriction = " AND step_id=CAST(:step AS uuid)" if step_id is not None else ""
        rows = [
            dict(row)
            for row in query(
                tx,
                """SELECT entry_id,revision,checkpoint_kind,
            attempt_id,step_id,step_index,input_set_id,data,archive_views FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND (CAST(:attempt AS uuid) IS NULL OR attempt_id=CAST(:attempt AS uuid))
            """
                + restriction
                + " ORDER BY revision",
                p=project,
                r=run_id,
                attempt=attempt_id,
                step=step_id,
            ).mappings()
        ]
        for set_id in {row["input_set_id"] for row in rows}:
            access.inputs(repo, tx, project, run_id, set_id)
        access.entry_privacy(tx, project, [row["entry_id"] for row in rows])
        return rows


def attempts(journal, run_id, stage):
    from .attempt_projection import project_attempts

    return project_attempts(journal.repo, journal.project, run_id, stage)


def turns(journal, root):
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        result = {
            row["turn_index"]: dict(row)
            for row in query(
                tx,
                "SELECT * FROM evaluation_model_turns WHERE project_id=:p AND root_invocation_id=:i ORDER BY turn_index",
                p=journal.project,
                i=root["invocation_id"],
            ).mappings()
        }
    for fact in facts(journal.repo, journal.project, root["run_id"], attempt_id=root["invocation_id"]):
        if fact["step_index"] is None:
            continue
        if "physical" in fact["data"]:
            result[fact["step_index"]] = identity_row(fact["data"]["physical"])
        if fact["step_index"] in result:
            result[fact["step_index"]].update(identity_row(fact["data"].get("state", {})))
    return sorted(result.values(), key=lambda row: row["turn_index"])


def events(journal, root, *, turn_index=None):
    restriction = " AND source_turn_index=:turn" if turn_index is not None else ""
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        old = [
            dict(row)
            for row in query(
                tx,
                """SELECT * FROM evaluation_invocation_events
            WHERE project_id=:p AND invocation_id=:i"""
                + restriction
                + " ORDER BY sequence",
                p=journal.project,
                i=root["invocation_id"],
                turn=turn_index,
            ).mappings()
        ]
    result = list(old)
    base = max((row["sequence"] for row in old), default=0)
    for fact in facts(
        journal.repo,
        journal.project,
        root["run_id"],
        attempt_id=root["invocation_id"],
        step_id=uuid5(root["invocation_id"], f"source-turn:{turn_index}") if turn_index is not None else None,
    ):
        for index, view in enumerate(fact["archive_views"]):
            event = view.get("event")
            if event is not None:
                record_id = UUID(view["record_id"])
                result.append(
                    dict(
                        project_id=journal.project,
                        invocation_id=root["invocation_id"],
                        event_id=uuid5(record_id, "event/3"),
                        record_id=record_id,
                        event_kind=event["kind"],
                        source_turn_index=event.get("source_turn_index"),
                        sequence=base + fact["revision"] * 128 + index,
                    )
                )
    return [row for row in result if turn_index is None or row.get("source_turn_index") == turn_index]


def turn(journal, root, index):
    """Address one physical step; preparing N turns never walks 1+...+N history."""
    with _read_connection(journal.repo) as tx:
        journal.authorize(tx)
        old = one(
            tx,
            """SELECT * FROM evaluation_model_turns
            WHERE project_id=:p AND root_invocation_id=:i AND turn_index=:n""",
            p=journal.project,
            i=root["invocation_id"],
            n=index,
        )
    result = dict(old) if old else None
    step_id = old["invocation_id"] if old else uuid5(root["invocation_id"], f"source-turn:{index}")
    for fact in facts(
        journal.repo, journal.project, root["run_id"], attempt_id=root["invocation_id"], step_id=step_id
    ):
        if "physical" in fact["data"]:
            result = identity_row(fact["data"]["physical"])
        if result is not None:
            result.update(identity_row(fact["data"].get("state", {})))
    return result
