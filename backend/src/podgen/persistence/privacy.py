"""Privacy follows provenance AND aliases of identical bytes. Quality never restores access."""

from .database import query
from .privacy_graph import hash_batches, privacy_closure


def revoke_content(tx, project_id, revision_id, event_id, reason):
    closure = privacy_closure(tx, project_id, artifact_ids=[revision_id])
    for hashes in hash_batches({value for kind, value in closure if kind == "blob"}):
        query(
            tx,
            """INSERT INTO blob_access_revocations(project_id,sha256,event_id,reason)
               SELECT :p,checksum,:id,:reason FROM unnest(CAST(:hashes AS text[])) AS marked(checksum)
               ON CONFLICT DO NOTHING""",
            p=project_id,
            hashes=hashes,
            id=event_id,
            reason=reason,
        )

    records = [value for kind, value in closure if kind == "runtime"]
    for offset in range(0, len(records), 512):
        query(
            tx,
            """INSERT INTO execution_record_withdrawals(project_id,record_id,event_id,reason)
            SELECT project_id,record_id,:event,:reason FROM runtime_records
            WHERE project_id=:p AND record_id=ANY(CAST(:records AS uuid[]))
              AND execution_input_set_id IS NOT NULL ON CONFLICT DO NOTHING""",
            p=project_id,
            records=records[offset : offset + 512],
            event=event_id,
            reason=reason,
        )

    entries = [value for kind, value in closure if kind == "entry"]
    for offset in range(0, len(entries), 512):
        query(
            tx,
            """INSERT INTO execution_entry_denials(project_id,entry_id,event_id,reason)
            SELECT :p,entry_id,:event,:reason FROM unnest(CAST(:entries AS uuid[])) AS reached(entry_id)
            ON CONFLICT DO NOTHING""",
            p=project_id,
            entries=entries[offset : offset + 512],
            event=event_id,
            reason=reason,
        )
