from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.audio.fidelity.contracts import prefix
from podgen.runtime.evaluation.audio.fidelity.export_results import export_results
from podgen.runtime.evaluation.audio.immutable_export import write_bytes
from tests.audio_cases import MemoryJournal


def result_fixture():
    journal = MemoryJournal()
    config = SimpleNamespace(audit_id=uuid4())
    root = prefix(config.audit_id)
    key = root + ":processing:1"
    journal.put(key, dict(source_receipt_ref=journal.ref(root + ":receipt").model_dump(mode="json")))
    blob = journal.blob(b"immutable audio fixture", "audio/wav")
    journal.put(key + ":result", dict(report=dict(candidates=[dict(candidate_id="one", clip_blob=blob)])))
    return journal, config


def test_missing_clip_resumes_without_rewriting_existing_json(tmp_path):
    j, c = result_fixture()
    pointer, _ = export_results(j, c, tmp_path)
    path = tmp_path / pointer["current_result"]["path"]
    clip = path.parent / "one.wav"
    original = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    clip.unlink()
    export_results(j, c, tmp_path)
    assert clip.read_bytes() == b"immutable audio fixture"
    assert path.read_bytes() == original and path.stat().st_mtime_ns == mtime
    clip.write_bytes(b"foreign")
    with pytest.raises(Conflict):
        export_results(j, c, tmp_path)
    assert clip.read_bytes() == b"foreign"


def test_failed_atomic_publish_does_not_leave_partial_destination(monkeypatch, tmp_path):
    path = tmp_path / "artifact"

    def interrupted(*args):
        raise OSError("injected interrupted rename")

    with monkeypatch.context() as patch:
        patch.setattr("podgen.runtime.evaluation.audio.immutable_export.os.replace", interrupted)
        with pytest.raises(OSError):
            write_bytes(path, b"complete content")
    assert not path.exists() and not list(tmp_path.glob(".artifact-*"))
    write_bytes(path, b"complete content")
    before = path.stat().st_mtime_ns
    write_bytes(path, b"complete content")
    assert path.stat().st_mtime_ns == before
    with pytest.raises(Conflict):
        write_bytes(path, b"different")
