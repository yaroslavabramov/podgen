"""Apply the real migration chain to a separate PostgreSQL database, retaining a v1 artifact."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.database import Database, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository


def test_fresh_database_and_upgrade_preserve_canonical_history(database, tmp_path):
    if os.environ.get("PODGEN_TEST_MIGRATIONS") != "1":
        pytest.skip("Set PODGEN_TEST_MIGRATIONS=1 for real CREATE DATABASE migration round-trip")
    name = "podgen_migration_" + uuid4().hex
    admin = create_engine(database.engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    root = Path(__file__).resolve().parents[1]
    with admin.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    db = Database(database.engine.url.set(database=name).render_as_string(hide_password=False))
    env = os.environ | {"PODGEN_DATABASE_URL": db.engine.url.render_as_string(hide_password=False)}

    def migrate(revision):
        subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", revision],
            env=env,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    try:
        migrate("0001_core")
        project, actor = uuid4(), uuid4()
        with db.transaction() as tx:
            query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
            query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=actor)
        repo = Repository(db, FileObjectStore(tmp_path / "objects"), actor)
        producer = CodeProducer(
            kind="code", component="migration-test", build_id="test-only", operation_id=uuid4()
        )
        before = repo.create(
            project,
            "Policy",
            {
                "policy_kind": "data",
                "rules": {"preserve": "ТЕСТ 🛰️е\u0301", "decimal": "9007199254740993.0001"},
            },
            producer=producer,
            operation_id=uuid4(),
        )
        # Model pre-upgrade access withdrawal with two existing owners of the same bytes.
        from io import BytesIO

        from podgen.domain.canonical import content_hash
        from podgen.domain.contracts.common import ArtifactRef
        from podgen.domain.errors import AccessDenied
        from podgen.persistence.database import json_text
        from podgen.persistence.repository import timestamp

        handle = repo.objects.put(project, BytesIO(b"SYNTHETIC legacy revoked bytes"), "text/plain")
        old_refs = []
        with db.transaction() as tx:
            query(
                tx,
                "INSERT INTO blobs VALUES(:p,:key,:sha,:n,:m)",
                p=project,
                key=handle.storage_key,
                sha=handle.sha256,
                n=handle.byte_length,
                m=handle.media_type,
            )
            for _index in range(2):
                ref = ArtifactRef(kind="DiagnosticArtifact", artifact_id=uuid4(), revision_id=uuid4())
                old_refs.append(ref)
                body = dict(
                    code="synthetic-legacy", message="Legacy privacy migration regression", related_refs=[]
                )
                checksum = content_hash(
                    ref.kind, "1.0", body, [], [dict(purpose="/legacy", hash=handle.sha256)]
                )
                query(tx, "INSERT INTO artifacts VALUES(:p,:a,:k)", p=project, a=ref.artifact_id, k=ref.kind)
                query(
                    tx,
                    """INSERT INTO artifact_revisions VALUES(:p,:r,:a,:k,NULL,'1.0',CAST(:body AS jsonb),'[]'::jsonb,CAST(:producer AS jsonb),:hash,:time,'project',:op,:fp)""",
                    p=project,
                    r=ref.revision_id,
                    a=ref.artifact_id,
                    k=ref.kind,
                    body=json_text(body),
                    producer=json_text(producer),
                    hash=checksum,
                    time=timestamp(),
                    op=uuid4(),
                    fp="test-only",
                )
                query(
                    tx,
                    "INSERT INTO artifact_blobs VALUES(:p,:r,:key,'/legacy')",
                    p=project,
                    r=ref.revision_id,
                    key=handle.storage_key,
                )
            query(
                tx,
                "INSERT INTO invalidations VALUES(:p,:id,:r,'SYNTHETIC earlier access withdrawal','access')",
                p=project,
                id=uuid4(),
                r=old_refs[0].revision_id,
            )
        # Old public wrappers of evaluator bytes must be protected by the new migration as well.
        from podgen.domain.contracts.common import RuntimeRef

        author_blob = repo.objects.put(project, BytesIO(b"SYNTHETIC legitimate author source"), "text/plain")
        gold_blob = repo.objects.put(project, BytesIO(b"SYNTHETIC legacy evaluator-only bytes"), "text/plain")
        author_runtime = RuntimeRef(record_kind="user_input", record_id=uuid4())
        hidden_runtime = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        old_wrapper = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        with db.transaction() as tx:
            for blob in (author_blob, gold_blob):
                query(
                    tx,
                    "INSERT INTO blobs VALUES(:p,:key,:sha,:n,:m)",
                    p=project,
                    key=blob.storage_key,
                    sha=blob.sha256,
                    n=blob.byte_length,
                    m=blob.media_type,
                )
            for ref, body, audience in [
                (author_runtime, {"blob": author_blob.model_dump(mode="json")}, "project"),
                (
                    hidden_runtime,
                    {
                        "blob": gold_blob.model_dump(mode="json"),
                        "author_source": author_runtime.model_dump(mode="json"),
                    },
                    "evaluator",
                ),
                (old_wrapper, {"blob": gold_blob.model_dump(mode="json")}, "project"),
            ]:
                query(
                    tx,
                    "INSERT INTO runtime_records VALUES(:p,:r,:k,CAST(:body AS jsonb),:aud)",
                    p=project,
                    r=ref.record_id,
                    k=ref.record_kind,
                    body=json_text(body),
                    aud=audience,
                )
        migrate("head")
        assert repo.get(project, before.ref) == before
        with pytest.raises(AccessDenied):
            repo.read_runtime_blob(project, old_wrapper, gold_blob)
        with pytest.raises(AccessDenied):
            repo.read_runtime_blob(project, hidden_runtime, gold_blob)
        assert (
            repo.read_runtime_blob(project, author_runtime, author_blob)
            == b"SYNTHETIC legitimate author source"
        )
        evaluation = Repository(db, repo.objects, actor, purpose="evaluation")
        assert (
            evaluation.read_runtime_blob(project, hidden_runtime, gold_blob)
            == b"SYNTHETIC legacy evaluator-only bytes"
        )

        for ref in old_refs:
            with pytest.raises(AccessDenied):
                repo.read_blob(project, ref, handle)
        with pytest.raises(AccessDenied):
            repo.save_blob(project, BytesIO(b"SYNTHETIC legacy revoked bytes"), "text/plain")
        after = repo.create(
            project,
            "Policy",
            {"policy_kind": "data", "rules": {"full_unicode": "\0🛰️"}},
            producer=producer,
            operation_id=uuid4(),
        )
        assert repo.get(project, after.ref).payload["rules"]["full_unicode"] == "\0🛰️"
        migrate("head")
        assert repo.get(project, before.ref).content_hash == before.content_hash
    finally:
        db.engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()
