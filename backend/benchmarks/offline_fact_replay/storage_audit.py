"""Physical storage evidence, collected outside the measured product operation."""

LEGACY_EXECUTION = (
    "evaluation_model_invocations",
    "evaluation_model_turns",
    "evaluation_invocation_events",
    "execution_provider_calls",
    "execution_tool_results",
)
TABLES = LEGACY_EXECUTION + (
    "runtime_records",
    "runtime_runtime_refs",
    "runtime_blob_refs",
    "evaluator_blob_classifications",
    "blobs",
    "execution_entries",
    "execution_heads",
    "execution_objects",
    "semantic_execution_links",
)


def snapshot(database, project, run_id):
    # A raw connection avoids adding audit SQL to SQLAlchemy's product counters.
    # The replay owns this database; server phase counters reset before the run.
    connection = database.engine.raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute(
                "SELECT execution_storage FROM evaluation_writing_runs WHERE project_id=%s AND run_id=%s",
                (project, run_id),
            )
            row = cursor.fetchone()
            result = dict(storage=row[0] if row else None, tables={})
            for table in TABLES:
                # Only fixed identifiers from TABLES enter this SQL expression.
                cursor.execute(f"SELECT count(*) FROM {table} WHERE project_id=%s", (project,))
                stats = dict(rows=cursor.fetchone()[0])
                if table in LEGACY_EXECUTION:
                    cursor.execute(
                        f"""SELECT md5(coalesce(string_agg(value, '' ORDER BY value), ''))
                        FROM (SELECT md5(row_to_json(t)::text) AS value FROM {table} t
                              WHERE project_id=%s) AS fingerprints""",
                        (project,),
                    )
                    stats["fingerprint"] = cursor.fetchone()[0]
                result["tables"][table] = stats
            for table in ("blobs", "execution_objects"):
                cursor.execute(
                    f"SELECT coalesce(sum(byte_length),0) FROM {table} WHERE project_id=%s", (project,)
                )
                result["tables"][table]["registered_bytes"] = int(cursor.fetchone()[0])
            return result
    finally:
        connection.rollback()
        connection.close()


def verify(before, after):
    assert after["storage"] == "private/3", ("replay_did_not_use_private_storage", after["storage"])
    changed = [table for table in LEGACY_EXECUTION if before["tables"][table] != after["tables"][table]]
    assert not changed, ("private_execution_modified_legacy_tables", changed)
    return dict(
        scope="project rows; audit SQL excluded from product timings and query counters",
        legacy_execution_unchanged=True,
        before=before,
        after=after,
        row_growth={
            table: after["tables"][table]["rows"] - before["tables"][table]["rows"] for table in TABLES
        },
    )
