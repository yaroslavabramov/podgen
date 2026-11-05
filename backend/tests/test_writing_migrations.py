"""A real 0010 journal survives 0011 with a same-run/stage/original-attempt FK."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from podgen.persistence.database import Database, one, query


def test_legacy_invocation_upgrade_and_repair_parent_constraint(database):
    if os.environ.get("PODGEN_TEST_MIGRATIONS") != "1":
        pytest.skip("Set PODGEN_TEST_MIGRATIONS=1 for real migration round-trip")
    name = "podgen_writing_migration_" + uuid4().hex
    admin = create_engine(database.engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    db = Database(database.engine.url.set(database=name).render_as_string(hide_password=False))
    root = Path(__file__).resolve().parents[1]
    env = os.environ | {"PODGEN_DATABASE_URL": db.engine.url.render_as_string(hide_password=False)}

    def migrate(revision):
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
        )

    project, actor, record, run, other_run, original, other_review, draft = [uuid4() for _ in range(8)]
    try:
        migrate("0010_writing_evaluation")
        with db.transaction() as tx:
            query(tx, "INSERT INTO projects VALUES(:p,'tenant')", p=project)
            query(
                tx,
                "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution','{}'::jsonb,'evaluator')",
                p=project,
                r=record,
            )
            for run_id in (run, other_run):
                query(
                    tx,
                    "INSERT INTO evaluation_writing_runs(project_id,run_id,actor_id,input_fingerprint,manifest_id,status) VALUES(:p,:r,:a,'legacy-fingerprint',:m,'needs_attention')",
                    p=project,
                    r=run_id,
                    a=actor,
                    m=record,
                )
            for invocation, run_id, stage in (
                (original, run, "review"),
                (other_review, other_run, "review"),
                (draft, run, "draft"),
            ):
                query(
                    tx,
                    "INSERT INTO evaluation_model_invocations(project_id,run_id,invocation_id,stage,status,request_record_id,input_tokens,output_tokens) VALUES(:p,:r,:i,:s,'validation_failed',:q,100,123)",
                    p=project,
                    r=run_id,
                    i=invocation,
                    s=stage,
                    q=record,
                )
        migrate("head")
        with db.transaction() as tx:
            row = one(tx, "SELECT * FROM evaluation_model_invocations WHERE invocation_id=:i", i=original)
            assert row["attempt_index"] == 0 and row["parent_invocation_id"] is None
            assert row["input_tokens"] == 100 and row["output_tokens"] == 123

        def insert(parent, attempt=1, invocation=None):
            with db.transaction() as tx:
                query(
                    tx,
                    "INSERT INTO evaluation_model_invocations(project_id,run_id,invocation_id,stage,status,request_record_id,attempt_index,parent_invocation_id) VALUES(:p,:r,:i,'review','prepared',:q,:attempt,:parent)",
                    p=project,
                    r=run,
                    i=invocation or uuid4(),
                    q=record,
                    attempt=attempt,
                    parent=parent,
                )

        for parent in (other_review, draft, uuid4()):
            with pytest.raises(IntegrityError):
                insert(parent)
        repaired = uuid4()
        insert(original, invocation=repaired)
        with pytest.raises(IntegrityError):
            insert(repaired, attempt=2)
        migrate("head")
    finally:
        db.engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()
