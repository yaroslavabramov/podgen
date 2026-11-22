"""Bounded legacy reads and compact private event indexes; no historical bodies."""

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.persistence.database import query
from podgen.persistence.private_journal import access
from podgen.persistence.read_authorization import _read_connection

BATCH = 64


def _private_projection(fields):
    # Names come exclusively from the versioned control projection in this module's caller.
    values = ",".join(f"'{name}',p.payload->'data'->'{name}'" for name in sorted(fields))
    receipts = ",".join(
        f"'{name}',CASE WHEN p.payload->'data'->'result'->'{name}'->>'read_id' IS NOT NULL "
        f"THEN jsonb_build_object('read_id',p.payload->'data'->'result'->'{name}'->'read_id') END"
        for name in ("passage", "material_passage")
    )
    return f"""(SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'reference',jsonb_build_object('record_kind',e.archive_views->(p.n::int-1)->'record_kind',
                                      'record_id',e.archive_views->(p.n::int-1)->'record_id'),
        'declared_kind',e.archive_views->(p.n::int-1)->'event'->'kind',
        'record',jsonb_build_object('event_kind',p.payload->'event_kind',
            'invocation_id',p.payload->'invocation_id','request_ref',p.payload->'request_ref',
            'data',jsonb_strip_nulls(jsonb_build_object({values},
                'result',jsonb_strip_nulls(jsonb_build_object({receipts})),
                'search_delivery_identity',p.payload->'data'->'search_delivery_identity'))))
        ORDER BY p.n),'[]'::jsonb)
        FROM json_array_elements(e.archive_payloads) WITH ORDINALITY p(payload,n)
        WHERE p.payload->>'event_kind' IS NOT NULL) AS events"""


def records(journal, root, fields):
    # Retain the legacy prefix of an adopted run, with ordinary batch authorization.
    cursor = -1
    while True:
        with _read_connection(journal.repo) as tx:
            journal.authorize(tx)
            rows = list(
                query(
                    tx,
                    """SELECT record_id,sequence,event_kind
                FROM evaluation_invocation_events WHERE project_id=:p AND invocation_id=:a
                AND sequence>:cursor ORDER BY sequence LIMIT :batch""",
                    p=journal.project,
                    a=root["invocation_id"],
                    cursor=cursor,
                    batch=BATCH,
                ).mappings()
            )
        if not rows:
            break
        references = [RuntimeRef(record_kind="tool_execution", record_id=row["record_id"]) for row in rows]
        with journal.read_operation():
            values = journal.read_many(references)
        for row, reference, record in zip(rows, references, values, strict=True):
            yield reference, row["event_kind"], record
        cursor = rows[-1]["sequence"]

    projection = _private_projection(fields)
    with _read_connection(journal.repo) as tx:
        access.run_access(journal.repo, tx, journal.project, root["run_id"])
        end = query(
            tx,
            """SELECT COALESCE(max(revision),0) FROM execution_entries
            WHERE project_id=:p AND run_id=:r AND attempt_id=:a""",
            p=journal.project,
            r=root["run_id"],
            a=root["invocation_id"],
        ).scalar_one()
    cursor = 0
    while cursor < end:
        with _read_connection(journal.repo) as tx:
            access.run_access(journal.repo, tx, journal.project, root["run_id"])
            rows = list(
                query(
                    tx,
                    f"""SELECT e.entry_id,e.input_set_id,e.revision,e.projection_version,
                    json_array_length(e.archive_payloads)=jsonb_array_length(e.archive_views) AS balanced,
                    {projection}
                FROM execution_entries e WHERE project_id=:p AND run_id=:r AND attempt_id=:a
                AND revision>:cursor AND revision<=:end ORDER BY revision LIMIT :batch""",
                    p=journal.project,
                    r=root["run_id"],
                    a=root["invocation_id"],
                    cursor=cursor,
                    end=end,
                    batch=BATCH,
                ).mappings()
            )
            for set_id in {row["input_set_id"] for row in rows}:
                access.inputs(journal.repo, tx, journal.project, root["run_id"], set_id)
            access.entry_privacy(tx, journal.project, [row["entry_id"] for row in rows])
        if not rows:
            break
        for row in rows:
            if row["projection_version"] != 3 or not row["balanced"]:
                raise ContractError("unsupported_private_archive_projection")
            for item in row["events"]:
                record = item["record"]
                if record["event_kind"] != item["declared_kind"]:
                    raise Conflict("research_history_event_binding_changed")
                yield RuntimeRef.model_validate(item["reference"]), item["declared_kind"], record
        cursor = rows[-1]["revision"]
