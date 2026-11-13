"""Counting storage/SQL doubles: real Repository.save_blob, no PostgreSQL or network."""

import base64
import hashlib
import io
import shutil
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock
from uuid import uuid4

from botocore.exceptions import ClientError


def digest(raw):
    return base64.b64encode(raw).decode("ascii")


def failure(code):
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": int(code) if code.isdigit() else 400},
        },
        "S3",
    )


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class BlobDatabase:
    def __init__(self):
        self.rows = {}
        self.queries = Counter()
        self.role, self.classified, self.revoked = "owner", False, False
        self.lock = RLock()
        self.active = ContextVar("storage_test_tx", default=False)
        self.fail_commit = False

    @contextmanager
    def transaction(self):
        with self.lock:
            token = self.active.set(True)
            before = dict(self.rows)
            try:
                yield self
                if self.fail_commit and self.rows != before:
                    raise RuntimeError("commit failed")
            except BaseException:
                self.rows = before
                raise
            finally:
                self.active.reset(token)

    def execute(self, statement, params):
        sql = " ".join(str(statement).split())
        self.queries[sql] += 1
        if sql.startswith("SELECT role FROM project_members"):
            return Rows([{"role": self.role}] if self.role else [])
        if sql == (
            "SELECT EXISTS(SELECT 1 FROM evaluator_blob_classifications WHERE project_id=:p AND sha256=:sha) AS evaluator_only, "
            "EXISTS(SELECT 1 FROM blob_access_revocations WHERE project_id=:p AND sha256=:sha) AS access_revoked"
        ):
            return Rows([{"evaluator_only": self.classified, "access_revoked": self.revoked}])
        if sql.startswith("SELECT 1 FROM evaluator_blob_classifications"):
            return Rows([{"exists": 1}] if self.classified else [])
        if sql.startswith("SELECT 1 FROM blob_access_revocations"):
            return Rows([{"exists": 1}] if self.revoked else [])
        if sql.startswith("SELECT * FROM blobs"):
            return Rows(
                [
                    r
                    for (project, key), r in self.rows.items()
                    if project == params["p"]
                    and (
                        key == params["k"]
                        if "k" in params
                        else (r["sha256"], r["media_type"]) == (params["sha"], params["m"])
                    )
                ]
            )
        if sql.startswith("INSERT INTO blobs"):
            duplicate = any(
                (p, r["sha256"], r["media_type"]) == (params["p"], params["sha"], params["media"])
                for (p, _), r in self.rows.items()
            )
            if not duplicate:
                self.rows[(params["p"], params["key"])] = dict(
                    project_id=params["p"],
                    storage_key=params["key"],
                    sha256=params["sha"],
                    byte_length=params["n"],
                    media_type=params["media"],
                )
            return Rows([])
        raise AssertionError(sql)


class CountingBody:
    def __init__(self, source, counts):
        self.source, self.counts = source, counts

    def read(self, size=-1):
        data = self.source.read(size)
        self.counts["get_bytes"] += len(data)
        self.counts["largest_get_read"] = max(self.counts["largest_get_read"], len(data))
        return data

    def close(self):
        self.source.close()


class DiskS3:
    def __init__(self, root: Path, db=None):
        self.root, self.db = root, db
        root.mkdir()
        self.objects, self.uploads = {}, {}
        self.counts, self.lock = Counter(), RLock()
        self.head_barrier = None
        self.after_write = None
        self.after_head = None
        self.fail_part = False
        self.enforce_no_tx = True

    def _call(self, name):
        if self.db is not None and self.db.active.get():
            self.counts["network_in_tx"] += 1
            assert not self.enforce_no_tx, "S3 I/O inside SQL transaction"
        self.counts[name] += 1

    def _copy(self, source):
        path = self.root / uuid4().hex
        h, size = hashlib.sha256(), 0
        with path.open("wb") as target:
            while raw := source.read(1024 * 1024):
                target.write(raw)
                h.update(raw)
                size += len(raw)
        return path, size, digest(h.digest())

    def head_object(self, **kw):
        self._call("head")
        key = kw["Key"]
        with self.lock:
            item = self.objects.get(key)
        if item is None and self.head_barrier:
            self.head_barrier.wait(timeout=5)
        if self.after_head:
            self.after_head()
        if item is None:
            raise failure("404")
        return {k: v for k, v in item.items() if k != "path"}

    def put_object(self, **kw):
        self._call("put")
        assert kw["IfNoneMatch"] == "*" and kw["ChecksumAlgorithm"] == "SHA256"
        with self.lock:
            if kw["Key"] in self.objects:
                raise failure("412")
            path, length, sha = self._copy(kw["Body"])
            self.counts["upload_bytes"] += length
            if sha != kw["ChecksumSHA256"] or length != kw["ContentLength"]:
                raise failure("BadDigest")
            self.objects[kw["Key"]] = dict(
                path=path,
                ContentLength=length,
                ContentType=kw["ContentType"],
                Metadata=kw["Metadata"],
                ChecksumSHA256=sha,
                ChecksumType="FULL_OBJECT",
            )
            self.counts["versions"] += 1
        if self.after_write:
            self.after_write()
        return {"ChecksumSHA256": sha, "ChecksumType": "FULL_OBJECT"}

    def create_multipart_upload(self, **kw):
        self._call("create_multipart")
        assert kw["ChecksumAlgorithm"] == "SHA256" and kw["ChecksumType"] == "COMPOSITE"
        uid = uuid4().hex
        self.uploads[uid] = dict(request=kw, parts={})
        return {"UploadId": uid}

    def upload_part(self, **kw):
        self._call("upload_part")
        if self.fail_part:
            raise failure("ServiceUnavailable")
        path, length, sha = self._copy(kw["Body"])
        self.counts["upload_bytes"] += length
        assert sha == kw["ChecksumSHA256"] and length == kw["ContentLength"]
        self.uploads[kw["UploadId"]]["parts"][kw["PartNumber"]] = (path, sha)
        return {"ChecksumSHA256": sha, "ETag": f"opaque-{kw['PartNumber']}"}

    def complete_multipart_upload(self, **kw):
        self._call("complete_multipart")
        assert kw["IfNoneMatch"] == "*"
        with self.lock:
            if kw["Key"] in self.objects:
                raise failure("412")
            upload = self.uploads[kw["UploadId"]]
            path, hashes = self.root / uuid4().hex, []
            with path.open("wb") as target:
                for p in kw["MultipartUpload"]["Parts"]:
                    part, sha = upload["parts"][p["PartNumber"]]
                    assert sha == p["ChecksumSHA256"]
                    hashes.append(base64.b64decode(sha))
                    with part.open("rb") as source:
                        shutil.copyfileobj(source, target, 1024 * 1024)
            checksum = digest(hashlib.sha256(b"".join(hashes)).digest()) + f"-{len(hashes)}"
            self.objects[kw["Key"]] = dict(
                path=path,
                ContentLength=path.stat().st_size,
                ContentType=upload["request"]["ContentType"],
                Metadata=upload["request"]["Metadata"],
                ChecksumSHA256=checksum,
                ChecksumType="COMPOSITE",
            )
            self.counts["versions"] += 1
            self._remove_upload(kw["UploadId"])
        if self.after_write:
            self.after_write()
        return {"ChecksumSHA256": checksum, "ChecksumType": "COMPOSITE"}

    def _remove_upload(self, uid):
        for path, _ in self.uploads.pop(uid)["parts"].values():
            path.unlink()

    def abort_multipart_upload(self, **kw):
        self._call("abort_multipart")
        if kw["UploadId"] not in self.uploads:
            raise failure("NoSuchUpload")
        self._remove_upload(kw["UploadId"])
        return {}

    def get_object(self, **kw):
        self._call("get")
        return {"Body": CountingBody(self.objects[kw["Key"]]["path"].open("rb"), self.counts)}

    def upload_fileobj(self, source, bucket, key, ExtraArgs):
        # Exact old production entry point, used by the baseline measurement.
        self._call("upload_fileobj")
        path, length, sha = self._copy(source)
        self.counts["upload_bytes"] += length
        self.counts["versions"] += 1
        self.objects[key] = dict(
            path=path,
            ContentLength=length,
            ContentType=ExtraArgs["ContentType"],
            Metadata=ExtraArgs["Metadata"],
        )

    def legacy(self, key, raw, media):
        path, length, _ = self._copy(io.BytesIO(raw))
        self.objects[key] = dict(path=path, ContentLength=length, ContentType=media, Metadata={})
