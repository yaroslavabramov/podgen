"""Immutable, project-scoped blobs. Storage keys never confer authorization."""

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError


def verify(data: bytes, ref: BlobRef):
    if len(data) != ref.byte_length or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ContractError("blob_integrity_error")


class FileObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: UUID, key: str):
        prefix = f"{project_id}/"
        path = (self.root / key).resolve()
        if not key.startswith(prefix) or not path.is_relative_to(self.root / str(project_id)):
            raise ContractError("invalid_blob_scope")
        return path

    def put(self, project_id, stream, media_type, expected_sha256=None, expected_length=None):
        scope = self.root / str(project_id)
        scope.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=scope)
        size, checksum = 0, hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    checksum.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            sha = checksum.hexdigest()
            if expected_sha256 is not None and sha != expected_sha256:
                raise ContractError("blob_checksum_mismatch")
            if expected_length is not None and size != expected_length:
                raise ContractError("blob_length_mismatch")
            key = f"{project_id}/{sha}-{hashlib.sha256(media_type.encode()).hexdigest()[:16]}"
            final = self._path(project_id, key)
            # link is atomic and never overwrites an already finalized object.
            try:
                os.link(temporary, final)
            except FileExistsError:
                verify(
                    final.read_bytes(),
                    BlobRef(storage_key=key, sha256=sha, byte_length=size, media_type=media_type),
                )
            directory = os.open(scope, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return BlobRef(storage_key=key, sha256=sha, byte_length=size, media_type=media_type)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def read(self, project_id, ref):
        data = self._path(project_id, ref.storage_key).read_bytes()
        verify(data, ref)
        return data

    def iter_read(self, project_id, ref, *, block_size=1024 * 1024):
        checksum, size = hashlib.sha256(), 0
        with self._path(project_id, ref.storage_key).open("rb") as stream:
            while chunk := stream.read(block_size):
                checksum.update(chunk)
                size += len(chunk)
                yield chunk
        if size != ref.byte_length or checksum.hexdigest() != ref.sha256:
            raise ContractError("blob_integrity_error")


class S3ObjectStore:
    """Finalize with an unpredictable immutable key; DB publication happens afterwards."""

    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket

    def put(self, project_id, stream, media_type, expected_sha256=None, expected_length=None):
        with tempfile.TemporaryFile() as staged:
            checksum, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                staged.write(chunk)
                checksum.update(chunk)
                size += len(chunk)
            sha = checksum.hexdigest()
            if expected_sha256 is not None and sha != expected_sha256:
                raise ContractError("blob_checksum_mismatch")
            if expected_length is not None and size != expected_length:
                raise ContractError("blob_length_mismatch")
            staged.seek(0)
            key = f"{project_id}/{uuid4()}/{sha}"
            self.client.upload_fileobj(
                staged,
                self.bucket,
                key,
                ExtraArgs={"ContentType": media_type, "Metadata": {"sha256": sha, "byte-length": str(size)}},
            )
        ref = BlobRef(storage_key=key, sha256=sha, byte_length=size, media_type=media_type)
        self.read(project_id, ref)  # actual read-after-write, not trust in ETag
        return ref

    def read(self, project_id, ref):
        if not ref.storage_key.startswith(f"{project_id}/"):
            raise ContractError("invalid_blob_scope")
        result = self.client.get_object(Bucket=self.bucket, Key=ref.storage_key)
        try:
            data = result["Body"].read()
        finally:
            result["Body"].close()
        verify(data, ref)
        return data

    def iter_read(self, project_id, ref, *, block_size=1024 * 1024):
        if not ref.storage_key.startswith(f"{project_id}/"):
            raise ContractError("invalid_blob_scope")
        result = self.client.get_object(Bucket=self.bucket, Key=ref.storage_key)
        checksum, size = hashlib.sha256(), 0
        try:
            while chunk := result["Body"].read(block_size):
                checksum.update(chunk)
                size += len(chunk)
                yield chunk
        finally:
            result["Body"].close()
        if size != ref.byte_length or checksum.hexdigest() != ref.sha256:
            raise ContractError("blob_integrity_error")
