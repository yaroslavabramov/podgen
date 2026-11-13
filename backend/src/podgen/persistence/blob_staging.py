"""One bounded, operation-owned copy of incoming bytes, shared by storage and publication."""

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError

BLOCK_SIZE = 1024 * 1024


def verify(data: bytes, ref: BlobRef):
    if len(data) != ref.byte_length or hashlib.sha256(data).hexdigest() != ref.sha256:
        raise ContractError("blob_integrity_error")


@dataclass(frozen=True)
class StagedBlob:
    path: Path
    sha256: str
    byte_length: int
    media_type: str

    def reference(self, project_id):
        media_hash = hashlib.sha256(self.media_type.encode()).hexdigest()
        return BlobRef(
            storage_key=f"{project_id}/content-v1/{self.sha256}/{media_hash}",
            sha256=self.sha256,
            byte_length=self.byte_length,
            media_type=self.media_type,
        )

    def matches(self, ref):
        if (ref.sha256, ref.byte_length, ref.media_type) != (self.sha256, self.byte_length, self.media_type):
            raise ContractError("blob_integrity_error")


@contextmanager
def stage_blob(stream, media_type, expected_sha256=None, expected_length=None, *, directory=None):
    fd, filename = tempfile.mkstemp(prefix=".upload-", dir=directory)
    path = Path(filename)
    checksum, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := stream.read(BLOCK_SIZE):
                checksum.update(chunk)
                size += len(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        sha = checksum.hexdigest()
        if expected_sha256 is not None and sha != expected_sha256:
            raise ContractError("blob_checksum_mismatch")
        if expected_length is not None and size != expected_length:
            raise ContractError("blob_length_mismatch")
        yield StagedBlob(path, sha, size, media_type)
    finally:
        path.unlink(missing_ok=True)


class StagedObjectStore:
    def stage(self, project_id, stream, media_type, **kwargs):
        return stage_blob(stream, media_type, **kwargs)

    def put(self, project_id, stream, media_type, expected_sha256=None, expected_length=None):
        with self.stage(
            project_id,
            stream,
            media_type,
            expected_sha256=expected_sha256,
            expected_length=expected_length,
        ) as staged:
            return self.publish(project_id, staged)
