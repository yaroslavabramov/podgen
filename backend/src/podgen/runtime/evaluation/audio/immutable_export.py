"""Atomic publication and independent verification of immutable export artifacts."""

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from podgen.domain.errors import Conflict


def write_bytes(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".immutable-export.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").digest()
            if actual != hashlib.sha256(body).digest():
                raise Conflict("immutable_export_artifact_changed")
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".artifact-", delete=False) as output:
                temporary = Path(output.name)
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
