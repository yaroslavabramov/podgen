import json

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.audio.export import export
from podgen.runtime.evaluation.audio.export_directory import bind_directory
from tests.test_audio_evaluation import fixture


def snapshot(folder):
    return {
        str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
        for p in folder.rglob("*")
    }


def test_comparison_export_binds_before_writing_manifest_and_resumes_same_run(monkeypatch, tmp_path):
    journal, _, manifest, _ = fixture(monkeypatch)
    from podgen.runtime.evaluation.audio import export as module

    original = module.write_json

    def guarded_write(path, value):
        assert (tmp_path / "export-binding.json").is_file()
        return original(path, value)

    monkeypatch.setattr(module, "write_json", guarded_write)
    export(journal, tmp_path)
    binding = json.loads((tmp_path / "export-binding.json").read_text())
    assert binding == dict(
        contract="audio-comparison-export-binding/1",
        run_id=str(journal.run_id),
        manifest_ref=journal.ref("manifest").model_dump(mode="json"),
        manifest_hash=digest(manifest),
    )
    export(journal, tmp_path)


def test_other_run_export_is_rejected_without_changing_any_files(monkeypatch, tmp_path):
    first, *_ = fixture(monkeypatch)
    second, *_ = fixture(monkeypatch)
    export(first, tmp_path)
    before = snapshot(tmp_path)
    with pytest.raises(Conflict, match="bound_to_other_manifest"):
        export(second, tmp_path)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "state",
    [
        "unknown",
        "invalid-manifest",
        "other-manifest",
        "invalid-binding",
        "mismatched-binding",
        "tampered-manifest",
    ],
)
def test_unverified_nonempty_directory_is_untouched(monkeypatch, tmp_path, state):
    journal, _, manifest, _ = fixture(monkeypatch)
    (tmp_path / "keep.txt").write_bytes(b"unrelated user content")
    if state in ("invalid-manifest", "other-manifest", "tampered-manifest"):
        (tmp_path / "private").mkdir()
        (tmp_path / "private/manifest.json").write_text(
            "{" if state == "invalid-manifest" else json.dumps(dict(manifest, configuration_hash="other"))
        )
    if state in ("invalid-binding", "mismatched-binding", "tampered-manifest"):
        binding = dict(
            contract="audio-comparison-export-binding/1",
            run_id=str(journal.run_id),
            manifest_ref=journal.ref("manifest").model_dump(mode="json"),
            manifest_hash=digest(manifest),
        )
        if state == "mismatched-binding":
            binding["run_id"] = "other-run"
        (tmp_path / "export-binding.json").write_text(
            "{" if state == "invalid-binding" else json.dumps(binding)
        )
    before = snapshot(tmp_path)
    with pytest.raises(Conflict):
        export(journal, tmp_path)
    assert snapshot(tmp_path) == before


def test_exact_legacy_manifest_can_be_adopted_without_removing_unknown_leftovers(monkeypatch, tmp_path):
    journal, *_ = fixture(monkeypatch)
    export(journal, tmp_path)
    (tmp_path / "export-binding.json").unlink()
    leftover = tmp_path / "blind/S999.wav"
    leftover.write_bytes(b"unrelated recording")
    other = tmp_path / "private/other-run/original.wav"
    other.parent.mkdir()
    other.write_bytes(b"keep original")
    prior = (leftover.stat().st_mtime_ns, other.stat().st_mtime_ns)
    export(journal, tmp_path)
    assert leftover.read_bytes() == b"unrelated recording" and other.read_bytes() == b"keep original"
    assert (leftover.stat().st_mtime_ns, other.stat().st_mtime_ns) == prior
    assert (tmp_path / "export-binding.json").is_file()


@pytest.mark.parametrize("filename", ["export-binding.json", "batch.json"])
def test_all_audio_export_types_reject_unknown_nonempty_folder_and_wrong_legacy(filename, tmp_path):
    (tmp_path / "notes.txt").write_text("keep")
    before = snapshot(tmp_path)
    with pytest.raises(Conflict):
        bind_directory(tmp_path, filename, dict(ref="owned"), dict(contract="owned"))
    assert snapshot(tmp_path) == before
    (tmp_path / "private").mkdir()
    (tmp_path / "private/manifest.json").write_text('{"contract":"other"}')
    before = snapshot(tmp_path)
    with pytest.raises(Conflict):
        bind_directory(tmp_path, filename, dict(ref="owned"), dict(contract="owned"))
    assert snapshot(tmp_path) == before
