"""Atomic filesystem publication; existing refs retain their original paths."""

import hashlib
import os
from pathlib import Path
from uuid import UUID

from podgen.domain.errors import ContractError

from .blob_staging import BLOCK_SIZE, StagedObjectStore, stage_blob, verify


class FileObjectStore(StagedObjectStore):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: UUID, key: str):
        prefix = f"{project_id}/"
        path = (self.root / key).resolve()
        if not key.startswith(prefix) or not path.is_relative_to(self.root / str(project_id)):
            raise ContractError("invalid_blob_scope")
        return path

    def stage(self, project_id, stream, media_type, **kwargs):
        scope = self.root / str(project_id)
        scope.mkdir(exist_ok=True)
        return stage_blob(stream, media_type, directory=scope, **kwargs)

    def publish(self, project_id, staged):
        ref = staged.reference(project_id)
        final = self._path(project_id, ref.storage_key)
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(staged.path, final)
        except FileExistsError:
            pass
        self.verify_existing(project_id, ref, staged)
        # Persist each new directory entry as well as the final file's link.
        directory = final.parent
        while True:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            if directory == self.root:
                break
            directory = directory.parent
        return ref

    def verify_existing(self, project_id, ref, staged):
        staged.matches(ref)
        for _ in self.iter_read(project_id, ref):
            pass

    def read(self, project_id, ref):
        data = self._path(project_id, ref.storage_key).read_bytes()
        verify(data, ref)
        return data

    def iter_read(self, project_id, ref, *, block_size=BLOCK_SIZE):
        checksum, size = hashlib.sha256(), 0
        with self._path(project_id, ref.storage_key).open("rb") as stream:
            while chunk := stream.read(block_size):
                checksum.update(chunk)
                size += len(chunk)
                yield chunk
        if size != ref.byte_length or checksum.hexdigest() != ref.sha256:
            raise ContractError("blob_integrity_error")
