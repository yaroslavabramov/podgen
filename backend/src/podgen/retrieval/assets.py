"""Explicit model installation, checksum verification and atomic publication; no runtime downloads."""

import fcntl
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

from . import RetrievalUnavailable
from .config import ENCODER, RERANKER


def cache_root():
    return Path(os.environ.get("PODGEN_SEARCH_CACHE", Path.home() / ".cache/podgen/source-search"))


def model_directory(spec):
    return cache_root() / "models" / spec["revision"]


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(spec):
    directory = model_directory(spec)
    try:
        for name, expected in spec["files"].items():
            if checksum(directory / name) != expected:
                raise RetrievalUnavailable("source_search_model_checksum_mismatch")
    except OSError as error:
        raise RetrievalUnavailable("source_search_models_not_installed") from error
    return directory


def prepare():
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for spec in (ENCODER, RERANKER):
            directory = model_directory(spec)
            for name, expected in spec["files"].items():
                destination = directory / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.is_file() and checksum(destination) == expected:
                    continue
                # Orphan downloads are never usable as installed models.
                for orphan in destination.parent.glob(destination.name + ".*.part"):
                    orphan.unlink()
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=destination.parent, prefix=destination.name + ".", suffix=".part", delete=False
                    ) as output:
                        temporary = Path(output.name)
                        url = f"https://huggingface.co/{spec['repository']}/resolve/{spec['revision']}/{name}"
                        with urllib.request.urlopen(url, timeout=180) as response:
                            while block := response.read(1024 * 1024):
                                output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if checksum(temporary) != expected:
                        raise RetrievalUnavailable("source_search_model_checksum_mismatch")
                    os.replace(temporary, destination)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            verify(spec)
