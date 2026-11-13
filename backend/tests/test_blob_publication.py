"""Whole save operations, with disk-backed S3 and counted SQL; no database service."""

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.blob_staging import BLOCK_SIZE
from podgen.persistence.objects import FileObjectStore, S3ObjectStore
from podgen.persistence.repository import Repository
from tests.blob_store_fakes import BlobDatabase, DiskS3


class Stream:
    def __init__(self, raw, callback=None):
        self.source, self.total, self.callback = io.BytesIO(raw), 0, callback

    def read(self, size):
        assert 0 < size <= BLOCK_SIZE
        data = self.source.read(size)
        self.total += len(data)
        if self.callback:
            self.callback()
        return data


@pytest.fixture
def storage(tmp_path):
    db = BlobDatabase()
    s3 = DiskS3(tmp_path / "s3", db)
    repo = Repository(db, S3ObjectStore(s3, "bucket"), uuid4())
    return repo, s3, db, uuid4()


def save(storage, raw=b"Original bytes", media="text/plain", **kwargs):
    repo, _, _, project = storage
    source = Stream(raw)
    ref = repo.save_blob(project, source, media, **kwargs)
    assert source.total == len(raw)
    return ref


@pytest.mark.parametrize("length", [0, 100, 17 * 1024 * 1024])
def test_whole_save_and_duplicate_do_not_download_or_reupload(storage, length):
    _, s3, db, _ = storage
    raw = b"a" * length
    first = save(storage, raw)
    first_calls = dict(s3.counts)
    first_sql = sum(db.queries.values())
    assert save(storage, raw) == first
    assert len(s3.objects) == 1 and s3.counts["versions"] == 1
    assert s3.counts["upload_bytes"] == length and s3.counts["get"] == 0
    assert s3.counts["head"] == 2
    assert s3.counts["put"] == first_calls.get("put", 0)
    assert s3.counts["upload_part"] == first_calls.get("upload_part", 0)
    assert (first_sql, sum(db.queries.values()) - first_sql) == (10, 9)
    assert not s3.uploads and len(db.rows) == 1


def test_project_and_media_are_separate_identities(storage):
    repo, s3, db, project = storage
    a = save(storage)
    b = save(storage, media="application/octet-stream")
    c = repo.save_blob(uuid4(), Stream(b"Original bytes"), "text/plain")
    assert len({a.storage_key, b.storage_key, c.storage_key}) == 3
    assert len(db.rows) == len(s3.objects) == 3


def test_bad_input_is_rejected_before_network(storage):
    _, s3, _, _ = storage
    for options, message in [
        ({"expected_sha256": "0" * 64}, "checksum"),
        ({"expected_length": 99}, "length"),
    ]:
        with pytest.raises(ContractError, match=message):
            save(storage, **options)
    assert not s3.counts


@pytest.mark.parametrize("corruption", ["checksum", "length", "media", "multipart_boundary"])
def test_dedup_checks_server_integrity_never_overwrites(storage, corruption):
    _, s3, _, _ = storage
    raw = b"x" * (17 * 1024 * 1024) if corruption == "multipart_boundary" else b"data"
    ref = save(storage, raw)
    item = s3.objects[ref.storage_key]
    if corruption == "checksum":
        item["ChecksumSHA256"] = "wrong"
    elif corruption == "length":
        item["ContentLength"] += 1
    elif corruption == "media":
        item["ContentType"] = "application/json"
    else:
        item["Metadata"]["podgen-part-size"] = str(5 * 1024 * 1024)
    with pytest.raises(ContractError, match="blob_integrity_error"):
        save(storage, raw)
    assert s3.counts["versions"] == 1 and s3.counts["get"] == 0


def legacy(storage, raw):
    _, s3, db, project = storage
    key = f"{project}/{uuid4()}/{hashlib.sha256(raw).hexdigest()}"
    ref = BlobRef(
        storage_key=key, sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw), media_type="text/plain"
    )
    s3.legacy(key, raw, ref.media_type)
    db.rows[(project, key)] = dict(project_id=project, **ref.model_dump())
    return ref


def test_legacy_reuses_original_key_and_verifies_once_in_bounded_chunks(storage):
    _, s3, _, _ = storage
    raw = b"x" * (3 * BLOCK_SIZE + 7)
    ref = legacy(storage, raw)
    assert save(storage, raw) == ref
    assert s3.counts["head"] == s3.counts["get"] == 1
    assert s3.counts["get_bytes"] == len(raw)
    assert s3.counts["largest_get_read"] == BLOCK_SIZE
    assert s3.counts["put"] == 0 and len(s3.objects) == 1


def test_legacy_metadata_cannot_hide_corruption(storage):
    _, s3, _, _ = storage
    ref = legacy(storage, b"good")
    s3.objects[ref.storage_key]["path"].write_bytes(b"evil")
    s3.objects[ref.storage_key]["Metadata"]["sha256"] = ref.sha256
    with pytest.raises(ContractError, match="blob_integrity_error"):
        save(storage, b"good")
    assert s3.counts["put"] == 0


@pytest.mark.parametrize("length", [100, 17 * 1024 * 1024])
def test_concurrent_saves_share_one_published_object_and_abort_losing_multipart(storage, length):
    _, s3, db, _ = storage
    s3.head_barrier = Barrier(2)
    raw = b"x" * length
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save(storage, raw), range(2)))
    assert results[0] == results[1]
    assert len(db.rows) == len(s3.objects) == s3.counts["versions"] == 1
    assert not s3.uploads and s3.counts["get"] == 0
    if length > 1024 * 1024:
        assert s3.counts["abort_multipart"] == 1


@pytest.mark.parametrize("length", [100, 17 * 1024 * 1024])
def test_unknown_upload_outcome_reuses_same_object_without_resending(storage, length):
    _, s3, db, _ = storage

    def lost_ack():
        raise TimeoutError("response lost after complete write")

    s3.after_write = lost_ack
    with pytest.raises(TimeoutError):
        save(storage, b"x" * length)
    assert not db.rows and len(s3.objects) == 1
    uploaded = s3.counts["upload_bytes"]
    s3.after_write = None
    save(storage, b"x" * length)
    assert s3.counts["upload_bytes"] == uploaded == length
    assert not s3.uploads and s3.counts["get"] == 0


def test_failed_db_publication_retries_existing_content_address(storage):
    _, s3, db, _ = storage
    db.fail_commit = True
    with pytest.raises(RuntimeError, match="commit failed"):
        save(storage)
    assert not db.rows and len(s3.objects) == 1
    db.fail_commit = False
    save(storage)
    assert s3.counts["put"] == 1 and s3.counts["get"] == 0


def test_multipart_failure_aborts_owned_upload_and_publishes_nothing(storage):
    _, s3, db, _ = storage
    s3.fail_part = True
    with pytest.raises(ClientError):
        save(storage, b"x" * (17 * 1024 * 1024))
    assert not db.rows and not s3.objects and not s3.uploads
    assert s3.counts["abort_multipart"] == 1


@pytest.mark.parametrize("blocked", ["revoked", "classified", "role"])
@pytest.mark.parametrize("when", ["before", "staging", "upload", "existing_head"])
def test_fresh_authorization_before_network_and_before_return(storage, blocked, when):
    repo, s3, db, project = storage

    def withdraw():
        setattr(db, blocked, None if blocked == "role" else True)

    if when == "existing_head":
        save(storage)
        s3.after_head = withdraw
    elif when == "before":
        withdraw()
    elif when == "upload":
        s3.after_write = withdraw
    source = Stream(b"Original bytes", withdraw if when == "staging" else None)
    with pytest.raises(AccessDenied):
        repo.save_blob(project, source, "text/plain")
    if when in ("before", "staging"):
        assert not s3.counts
    elif when == "upload":
        assert not db.rows


def test_filesystem_storage_uses_same_staging_and_keeps_legacy_refs(tmp_path):
    store = FileObjectStore(tmp_path / "files")
    db = BlobDatabase()
    repo = Repository(db, store, uuid4())
    project = uuid4()
    first = repo.save_blob(project, Stream(b"bytes"), "text/plain")
    assert repo.save_blob(project, Stream(b"bytes"), "text/plain") == first
    assert store.read(project, first) == b"bytes"
    legacy_raw = b"historical file bytes"
    sha = hashlib.sha256(legacy_raw).hexdigest()
    old = BlobRef(
        storage_key=f"{project}/{sha}-{hashlib.sha256(b'text/plain').hexdigest()[:16]}",
        sha256=sha,
        byte_length=len(legacy_raw),
        media_type="text/plain",
    )
    store._path(project, old.storage_key).write_bytes(legacy_raw)
    db.rows[(project, old.storage_key)] = dict(project_id=project, **old.model_dump())
    assert repo.save_blob(project, Stream(legacy_raw), "text/plain") == old
    store._path(project, first.storage_key).write_bytes(b"wrong")
    with pytest.raises(ContractError, match="blob_integrity_error"):
        repo.save_blob(project, Stream(b"bytes"), "text/plain")
    assert not list(store.root.rglob(".upload-*"))


def test_file_publication_syncs_new_project_entry_in_store_root(tmp_path, monkeypatch):
    from podgen.persistence import file_objects

    store, project = FileObjectStore(tmp_path / "files"), uuid4()
    original_open, original_sync = file_objects.os.open, file_objects.os.fsync
    directories, synced = {}, []

    def opened(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if flags == file_objects.os.O_RDONLY:
            directories[fd] = path
        return fd

    def sync(fd):
        if fd in directories:
            synced.append(directories[fd])
        return original_sync(fd)

    monkeypatch.setattr(file_objects.os, "open", opened)
    monkeypatch.setattr(file_objects.os, "fsync", sync)
    ref = store.put(project, Stream(b"bytes"), "text/plain")
    assert synced == [
        store._path(project, ref.storage_key).parent,
        store.root / str(project) / "content-v1",
        store.root / str(project),
        store.root,
    ]
