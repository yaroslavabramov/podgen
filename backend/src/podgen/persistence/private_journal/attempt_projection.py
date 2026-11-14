"""Address one stage's attempt headers and fold only its compact state facts."""

from podgen.persistence.database import query
from podgen.persistence.execution.reads import current_inputs
from podgen.persistence.read_authorization import _read_connection

from . import access
from .state import identity_row

BATCH = 64
BLOCKED = frozenset({"failed", "validation_failed", "outcome_unknown"})


def _authorize(repo, tx, project, run_id, rows):
    for set_id in {row["input_set_id"] for row in rows}:
        access.inputs(repo, tx, project, run_id, set_id)
    access.entry_privacy(tx, project, [row["entry_id"] for row in rows])


def _legacy_headers(tx, project, run_id, stage):
    cursor = -1
    while True:
        rows = [
            dict(row)
            for row in query(
                tx,
                """SELECT * FROM evaluation_model_invocations
            WHERE project_id=:p AND run_id=:r AND stage=:stage AND attempt_index>:cursor
            ORDER BY attempt_index LIMIT :batch""",
                p=project,
                r=run_id,
                stage=stage,
                cursor=cursor,
                batch=BATCH,
            ).mappings()
        ]
        yield from rows
        if len(rows) < BATCH:
            return
        cursor = rows[-1]["attempt_index"]


def _headers(repo, tx, project, run_id, stage, end):
    cursor = 0
    while cursor < end:
        rows = [
            dict(row)
            for row in query(
                tx,
                """SELECT entry_id,input_set_id,attempt_id,revision,
            data->'attempt' AS attempt FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND data->'attempt' IS NOT NULL
              AND data->'attempt'->>'stage'=:stage AND revision>:cursor AND revision<=:end
            ORDER BY revision LIMIT :batch""",
                p=project,
                r=run_id,
                stage=stage,
                cursor=cursor,
                end=end,
                batch=BATCH,
            ).mappings()
        ]
        _authorize(repo, tx, project, run_id, rows)
        yield from rows
        if len(rows) < BATCH:
            return
        cursor = rows[-1]["revision"]


def _fold(repo, tx, project, run_id, identities, end, result, starts):
    cursor = None
    while True:
        restriction = (
            " AND (attempt_id,revision)>(CAST(:cursor_id AS uuid),:cursor_revision)" if cursor else ""
        )
        rows = [
            dict(row)
            for row in query(
                tx,
                """SELECT entry_id,input_set_id,attempt_id,revision,
            step_index,data->'state' AS state FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND attempt_id=ANY(CAST(:ids AS uuid[]))
              AND revision<=:end"""
                + restriction
                + " ORDER BY attempt_id,revision LIMIT :batch",
                p=project,
                r=run_id,
                ids=identities,
                end=end,
                batch=BATCH,
                cursor_id=cursor[0] if cursor else None,
                cursor_revision=cursor[1] if cursor else 0,
            ).mappings()
        ]
        _authorize(repo, tx, project, run_id, rows)
        for fact in rows:
            identity = fact["attempt_id"]
            if fact["revision"] < starts.get(identity, 0):
                continue
            row = result[identity]
            change = identity_row(fact["state"] or {})
            if fact["step_index"] is None:
                row.update(change)
            elif "status" in change and row["status"] != "completed":
                status = change.get("status")
                row["status"] = status if status in BLOCKED else "tool_running"
        if len(rows) < BATCH:
            return
        cursor = rows[-1]["attempt_id"], rows[-1]["revision"]


@current_inputs
def project_attempts(repo, project, run_id, stage):
    # A local metadata operation owns one snapshot. No full payload, archive
    # views, unrelated stage facts or permission cache crosses this boundary.
    with _read_connection(repo) as tx:
        access.run_access(repo, tx, project, run_id)
        end = query(
            tx,
            "SELECT COALESCE(max(revision),0) FROM execution_entries WHERE project_id=:p AND run_id=:r",
            p=project,
            r=run_id,
        ).scalar_one()
        result = {row["invocation_id"]: row for row in _legacy_headers(tx, project, run_id, stage)}
        starts = {}
        for fact in _headers(repo, tx, project, run_id, stage, end):
            row = identity_row(fact["attempt"])
            identity = row["invocation_id"]
            result[identity], starts[identity] = row, fact["revision"]
        identities = sorted(result, key=str)
        for offset in range(0, len(identities), BATCH):
            _fold(repo, tx, project, run_id, identities[offset : offset + BATCH], end, result, starts)
        # The return contract contains all requested attempt descriptors; working
        # memory is their size plus one bounded page, not the run's fact history.
        return sorted(result.values(), key=lambda row: row["attempt_index"])
