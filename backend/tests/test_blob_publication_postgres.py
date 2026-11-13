"""Targeted integration tests for the real unique constraint and post-I/O publication ACL."""

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one, query
from podgen.persistence.objects import S3ObjectStore
from podgen.persistence.repository import Repository
from tests.blob_store_fakes import DiskS3


def stored_repo(repo, tmp_path):
    s3 = DiskS3(tmp_path / "s3")
    actual = Repository(repo.database, S3ObjectStore(s3, "bucket"), repo.actor_id, purpose=repo.purpose)
    return actual, s3


def test_concurrent_save_blob_publishes_one_postgres_row_and_s3_object(repo, scope, tmp_path):
    actual, s3 = stored_repo(repo, tmp_path)
    s3.head_barrier = Barrier(2)
    project, _ = scope
    raw = b"SYNTHETIC same publication bytes"
    with ThreadPoolExecutor(max_workers=2) as pool:
        refs = list(pool.map(lambda _: actual.save_blob(project, io.BytesIO(raw), "text/plain"), range(2)))
    assert refs[0] == refs[1]
    with repo.database.transaction() as tx:
        assert one(tx, "SELECT count(*) AS n FROM blobs WHERE project_id=:p", p=project)["n"] == 1
    assert len(s3.objects) == s3.counts["versions"] == 1 and s3.counts["get"] == 0


def test_acl_withdrawal_during_s3_upload_cannot_publish_and_retry_reuses_object(repo, scope, tmp_path):
    actual, s3 = stored_repo(repo, tmp_path)
    project, _ = scope

    def withdraw():
        with repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=project,
                a=repo.actor_id,
            )

    s3.after_write = withdraw
    with pytest.raises(AccessDenied):
        actual.save_blob(project, io.BytesIO(b"SYNTHETIC staged bytes"), "text/plain")
    with repo.database.transaction() as tx:
        assert one(tx, "SELECT count(*) AS n FROM blobs WHERE project_id=:p", p=project)["n"] == 0
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=repo.actor_id)
    s3.after_write = None
    actual.save_blob(project, io.BytesIO(b"SYNTHETIC staged bytes"), "text/plain")
    assert s3.counts["put"] == 1 and s3.counts["get"] == 0


def test_existing_postgres_legacy_key_is_retained_without_upload(repo, scope, tmp_path):
    actual, s3 = stored_repo(repo, tmp_path)
    project, _ = scope
    raw = b"SYNTHETIC legacy bytes"
    ref = BlobRef(
        storage_key=f"{project}/{uuid4()}/legacy",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        media_type="text/plain",
    )
    s3.legacy(ref.storage_key, raw, ref.media_type)
    with repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO blobs VALUES(:p,:key,:sha,:n,:media)",
            p=project,
            key=ref.storage_key,
            sha=ref.sha256,
            n=ref.byte_length,
            media=ref.media_type,
        )
    assert actual.save_blob(project, io.BytesIO(raw), ref.media_type) == ref
    assert s3.counts["get"] == 1 and s3.counts["put"] == 0 and len(s3.objects) == 1
