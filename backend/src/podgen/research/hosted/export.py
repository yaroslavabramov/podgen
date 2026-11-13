"""Export one research archive without replacing another run's files."""

import json
import os
import tempfile
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict

from .service import manifest
from .state import exchanges, status


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def bind(path, binding):
    # A hard link publishes a complete file with create-if-absent semantics.
    # Stage outside the export directory so a crash cannot leave it falsely occupied.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(binding, stream)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if json.loads(path.read_text()) != binding:
                    raise Conflict("hosted_research_export_directory_mismatch") from None
        finally:
            temporary.unlink(missing_ok=True)


def export(journal, folder):
    folder = Path(folder)
    value = manifest(journal)
    binding = dict(run_id=str(journal.run_id), manifest_hash=digest(value))
    path = folder / "binding.json"
    if path.exists():
        if json.loads(path.read_text()) != binding:
            raise Conflict("hosted_research_export_directory_mismatch")
    elif folder.exists() and any(folder.iterdir()):
        raise Conflict("hosted_research_export_directory_not_empty")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        bind(path, binding)
    write_json(folder / "manifest.json", value)
    write_json(folder / "request.json", value["request"])
    (folder / "request.txt").write_text(value["request"]["body"]["input"])
    write_json(folder / "approval.json", journal.get("approval"))
    for key in exchanges(journal):
        directory = folder / key.replace(":", "-")
        write_json(directory / "intent.json", journal.get(key + ":intent"))
        receipt = journal.get(key + ":receipt")
        if receipt:
            write_json(directory / "receipt.json", receipt)
            (directory / "response.bin").write_bytes(
                journal.bytes(key + ":receipt", receipt["raw_response_blob"])
            )
    result = journal.get("result")
    if result:
        write_json(folder / "result.json", result)
        (folder / "report.md").write_bytes(journal.bytes("result", result["report_text_blob"]))
        write_json(folder / "reported-sources.json", result["material"]["reported_sources"])
    current = status(journal)
    write_json(folder / "status.json", current)
    return current
