"""Bounded archive metadata over legacy rows and immutable private checkpoints."""

from uuid import UUID, uuid5

from podgen.persistence.database import one, query
from podgen.persistence.execution.reads import current_inputs
from podgen.persistence.read_authorization import _read_connection

from . import access
from .state import identity_row

BATCH = 64


@current_inputs
def _limits(journal, root):
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, root["run_id"])
        return dict(
            one(
                tx,
                """SELECT
            COALESCE((SELECT max(sequence) FROM evaluation_invocation_events
              WHERE project_id=:p AND invocation_id=:a),0) AS legacy_sequence,
            COALESCE((SELECT max(turn_index) FROM evaluation_model_turns
              WHERE project_id=:p AND root_invocation_id=:a),-1) AS legacy_turn,
            COALESCE((SELECT max(revision) FROM execution_entries
              WHERE project_id=:p AND run_id=:r AND attempt_id=:a),0) AS revision""",
                p=journal.project,
                r=root["run_id"],
                a=root["invocation_id"],
            )
        )


def _authorize_entries(journal, tx, root, rows):
    for set_id in {row["input_set_id"] for row in rows}:
        access.inputs(journal.repo, tx, journal.project, root["run_id"], set_id)
    access.entry_privacy(tx, journal.project, [row["entry_id"] for row in rows])


@current_inputs
def _old_events(journal, root, cursor, end):
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, root["run_id"])
        return [
            dict(row)
            for row in query(
                tx,
                """SELECT * FROM evaluation_invocation_events
            WHERE project_id=:p AND invocation_id=:a AND sequence>:cursor AND sequence<=:end
            ORDER BY sequence LIMIT :batch""",
                p=journal.project,
                a=root["invocation_id"],
                cursor=cursor,
                end=end,
                batch=BATCH,
            ).mappings()
        ]


@current_inputs
def _event_entries(journal, root, cursor, end):
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, root["run_id"])
        rows = [
            dict(row)
            for row in query(
                tx,
                """SELECT entry_id,input_set_id,revision,archive_views
            FROM execution_entries WHERE project_id=:p AND run_id=:r AND attempt_id=:a
              AND revision>:cursor AND revision<=:end ORDER BY revision LIMIT :batch""",
                p=journal.project,
                r=root["run_id"],
                a=root["invocation_id"],
                cursor=cursor,
                end=end,
                batch=BATCH,
            ).mappings()
        ]
        _authorize_entries(journal, tx, root, rows)
        return rows


def _events(journal, root, limits):
    cursor = -1
    while cursor < limits["legacy_sequence"]:
        rows = _old_events(journal, root, cursor, limits["legacy_sequence"])
        if not rows:
            break
        cursor = rows[-1]["sequence"]
        yield from rows
    cursor = 0
    while cursor < limits["revision"]:
        rows = _event_entries(journal, root, cursor, limits["revision"])
        if not rows:
            break
        cursor = rows[-1]["revision"]
        # Each checkpoint has at most 128 compact view descriptors. No semantic
        # payload or database snapshot survives this generator's yield.
        for row in rows:
            for index, view in enumerate(row["archive_views"]):
                event = view.get("event")
                if event is not None:
                    record_id = UUID(view["record_id"])
                    yield dict(
                        project_id=journal.project,
                        invocation_id=root["invocation_id"],
                        event_id=uuid5(record_id, "event/3"),
                        record_id=record_id,
                        event_kind=event["kind"],
                        source_turn_index=event.get("source_turn_index"),
                        sequence=limits["legacy_sequence"] + row["revision"] * 128 + index,
                    )


def event_pages(journal, root):
    pending = []
    for event in _events(journal, root, _limits(journal, root)):
        pending.append(event)
        if len(pending) == BATCH:
            yield pending
            pending = []
    if pending:
        yield pending


@current_inputs
def _turn_indexes(journal, root, cursor, limits):
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, root["run_id"])
        # Limit each ordered source before merging, rather than sorting the
        # complete mixed history on every page. The private source uses the
        # (project,run,attempt,step_index,revision) index.
        return list(
            query(
                tx,
                """WITH old AS (
            SELECT turn_index FROM evaluation_model_turns
            WHERE project_id=:p AND root_invocation_id=:a AND turn_index>:cursor AND turn_index<=:old_end
            ORDER BY turn_index LIMIT :batch
          ), private AS (
            SELECT DISTINCT step_index AS turn_index FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND attempt_id=:a AND step_index>:cursor
              AND step_index IS NOT NULL AND revision<=:end
            ORDER BY step_index LIMIT :batch
          )
          SELECT turn_index FROM (SELECT turn_index FROM old UNION SELECT turn_index FROM private) candidates
          ORDER BY turn_index LIMIT :batch""",
                p=journal.project,
                r=root["run_id"],
                a=root["invocation_id"],
                cursor=cursor,
                old_end=limits["legacy_turn"],
                end=limits["revision"],
                batch=BATCH,
            ).scalars()
        )


@current_inputs
def _turn_batch(journal, root, indexes, limits):
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, root["run_id"])
        result = {
            row["turn_index"]: dict(row)
            for row in query(
                tx,
                """SELECT * FROM evaluation_model_turns
            WHERE project_id=:p AND root_invocation_id=:a AND turn_index=ANY(CAST(:turns AS integer[]))
              AND turn_index<=:old_end
            ORDER BY turn_index""",
                p=journal.project,
                a=root["invocation_id"],
                turns=indexes,
                old_end=limits["legacy_turn"],
            ).mappings()
        }
        cursor_turn, cursor_revision = -1, 0
        while limits["revision"]:
            # Fold only the selected turns, in each turn's original revision
            # order. Both the SQL result and the accumulated projection are bounded.
            rows = [
                dict(row)
                for row in query(
                    tx,
                    """SELECT entry_id,input_set_id,step_index,revision,
                data->'physical' AS physical,data->'state' AS state FROM execution_entries
                WHERE project_id=:p AND run_id=:r AND attempt_id=:a
                  AND step_index=ANY(CAST(:turns AS integer[])) AND revision<=:end
                  AND (step_index,revision)>(:cursor_turn,:cursor_revision)
                ORDER BY step_index,revision LIMIT :batch""",
                    p=journal.project,
                    r=root["run_id"],
                    a=root["invocation_id"],
                    turns=indexes,
                    end=limits["revision"],
                    cursor_turn=cursor_turn,
                    cursor_revision=cursor_revision,
                    batch=BATCH,
                ).mappings()
            ]
            if not rows:
                break
            _authorize_entries(journal, tx, root, rows)
            for row in rows:
                index = row["step_index"]
                if row["physical"] is not None:
                    result[index] = identity_row(row["physical"])
                if index in result and row["state"] is not None:
                    result[index].update(identity_row(row["state"]))
            cursor_turn, cursor_revision = rows[-1]["step_index"], rows[-1]["revision"]
            if len(rows) < BATCH:
                break
        return [result[index] for index in indexes if index in result]


def turn_pages(journal, root):
    limits, cursor = _limits(journal, root), -1
    while True:
        indexes = _turn_indexes(journal, root, cursor, limits)
        if not indexes:
            break
        cursor = indexes[-1]
        page = _turn_batch(journal, root, indexes, limits)
        if page:
            yield page
