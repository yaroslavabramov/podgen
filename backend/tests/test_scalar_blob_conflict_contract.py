"""Force a READ COMMITTED conflict after the post-storage binding inspection."""

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import BlobRef
from podgen.persistence import blob_publication
from podgen.persistence.database import query
from tests.test_artifact_publication import transaction_guard
from tests.test_blob_publication_postgres import stored_repo


@pytest.mark.parametrize("legacy_winner", [False, True])
def test_scalar_conflict_reads_only_the_missing_winner_and_verifies_outside_sql(
    repo, scope, tmp_path, monkeypatch, request, legacy_winner
):
    actual, s3 = stored_repo(repo, tmp_path)
    project = scope[0]
    raw = b"SYNTHETIC deterministic post-inspection blob conflict"
    legacy = BlobRef(
        storage_key=f"{project}/{uuid4()}/legacy-conflict",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        media_type="text/plain",
    )
    if legacy_winner:
        # Prepare the competing object before any victim SQL transaction starts.
        s3.legacy(legacy.storage_key, raw, legacy.media_type)
    transaction_guard(actual, monkeypatch, request.addfinalizer)
    entered, release = Event(), Event()
    candidates, returned, inspected, verified = [], [], [], []
    insert, inspect = blob_publication.insert_rows, blob_publication._inspect_batch
    verify = actual.objects.verify_existing

    def blocked_insert(tx, table, rows, **options):
        if table == "blobs" and rows and not candidates:
            row = rows[0]
            candidates.append(
                BlobRef(**{key: row[key] for key in ("storage_key", "sha256", "byte_length", "media_type")})
            )
            entered.set()
            assert release.wait(10)
        result = insert(tx, table, rows, **options)
        if table == "blobs" and rows:
            returned.append(result)
        return result

    def inspect_missing(repo, tx, project, staged):
        value = inspect(repo, tx, project, staged)
        inspected.append(tuple((item.sha256, item.media_type) for item in staged))
        return value

    def verify_winner(project, ref, staged):
        verified.append(ref)
        return verify(project, ref, staged)

    monkeypatch.setattr(blob_publication, "insert_rows", blocked_insert)
    monkeypatch.setattr(blob_publication, "_inspect_batch", inspect_missing)
    monkeypatch.setattr(actual.objects, "verify_existing", verify_winner)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(actual.save_blob, project, io.BytesIO(raw), "text/plain")
        try:
            assert entered.wait(10)
            winner = legacy if legacy_winner else candidates[0]
            with repo.database.transaction() as tx:
                query(
                    tx,
                    "INSERT INTO blobs VALUES(:p,:key,:sha,:length,:media)",
                    p=project,
                    key=winner.storage_key,
                    sha=winner.sha256,
                    length=winner.byte_length,
                    media=winner.media_type,
                )
        finally:
            release.set()
        assert pending.result(timeout=10) == winner
    # The branch is forced: the victim lost INSERT after its own inspection.
    assert returned == [[]]
    assert len(inspected) == (4 if legacy_winner else 3)
    assert verified == ([legacy] if legacy_winner else [])
    assert s3.counts["put"] == 1 and s3.counts["get"] == int(legacy_winner)
    with repo.database.read_transaction() as tx:
        assert query(tx, "SELECT count(*) FROM blobs WHERE project_id=:p", p=project).scalar_one() == 1
