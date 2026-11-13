"""Authorized, checksum-verified Unicode ranges without retaining a corpus in RAM."""

import codecs
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.errors import AccessDenied
from podgen.domain.references import walk_values

from .database import one
from .read_authorization import _read_connection, checked_node, object_io, read_operation

_CACHE = tempfile.TemporaryDirectory(prefix="podgen-unicode-")
_LOCK = Lock()


def _authorize_binding(repo, project, reference, blob):
    with _read_connection(repo) as tx:

        def check():
            if isinstance(reference, ArtifactRef):
                row = repo._resolve(tx, project, reference)
                if row["project_id"] != project or not one(
                    tx,
                    "SELECT 1 FROM artifact_blobs WHERE project_id=:p AND revision_id=:r AND storage_key=:k",
                    p=project,
                    r=reference.revision_id,
                    k=blob.storage_key,
                ):
                    raise AccessDenied()
            else:
                row = repo._runtime(tx, project, reference)
                if blob not in [
                    value for _, _, value in walk_values(row["payload"]) if isinstance(value, BlobRef)
                ]:
                    raise AccessDenied()
            repo._check_blob(tx, project, blob)
            return True

        checked_node(repo, tx, ("text_binding", project, reference, blob), check)


@contextmanager
def authorized_blob(repo, project, reference, blob):
    # SQL phase ends before yielding to the object reader. Remote object_io then
    # releases its snapshot; returning bytes always rechecks fresh authorization.
    with read_operation(repo):
        _authorize_binding(repo, project, reference, blob)
        yield
        _authorize_binding(repo, project, reference, blob)


class VerifiedText:
    def __init__(self, repo, project, reference, blob):
        self.repo, self.project, self.reference, self.blob = repo, project, reference, blob
        self.sha256 = blob.sha256
        self.path = Path(_CACHE.name) / blob.sha256

    def __eq__(self, other):
        if not isinstance(other, VerifiedText):
            return NotImplemented
        return (self.project, self.reference, self.blob, self.repo.actor_id) == (
            other.project,
            other.reference,
            other.blob,
            other.repo.actor_id,
        )

    def _ensure(self):
        if self.path.exists():
            return
        with _LOCK:
            if self.path.exists():
                return
            decoder = codecs.getincrementaldecoder("utf-8")()
            # Publish only after the complete raw stream passed checksum/length
            # verification and strict UTF-8 decoding. The cache is a projection.
            with tempfile.NamedTemporaryFile(dir=_CACHE.name, delete=False) as output:
                temporary = Path(output.name)
                try:
                    with object_io(self.repo):
                        for chunk in self.repo.objects.iter_read(self.project, self.blob):
                            output.write(decoder.decode(chunk).encode("utf-32-le"))
                    output.write(decoder.decode(b"", final=True).encode("utf-32-le"))
                    output.flush()
                    temporary.replace(self.path)
                finally:
                    temporary.unlink(missing_ok=True)

    def verify(self):
        with authorized_blob(self.repo, self.project, self.reference, self.blob):
            self._ensure()

    def __len__(self):
        if not self.path.exists():
            self.verify()
        return self.path.stat().st_size // 4

    def __getitem__(self, key):
        with authorized_blob(self.repo, self.project, self.reference, self.blob):
            self._ensure()
            length = self.path.stat().st_size // 4
            if isinstance(key, slice):
                start, stop, step = key.indices(length)
                if step != 1:
                    return str(self)[key]
            else:
                start = key + length if key < 0 else key
                if not 0 <= start < length:
                    raise IndexError(key)
                stop = start + 1
            with self.path.open("rb") as stream:
                stream.seek(start * 4)
                return stream.read(max(0, stop - start) * 4).decode("utf-32-le")

    def __str__(self):
        return self[:]

    def encode(self, encoding="utf-8"):
        return str(self).encode(encoding)

    def find(self, text, start=0):
        if not text:
            return min(max(0, start), len(self))
        cursor = max(0, start)
        while cursor < len(self):
            value = self[cursor : cursor + 16384 + len(text)]
            found = value.find(text)
            if found >= 0:
                return cursor + found
            cursor += 16384
        return -1


def verified_text(repo, project, reference, blob):
    return VerifiedText(repo, project, reference, blob)
