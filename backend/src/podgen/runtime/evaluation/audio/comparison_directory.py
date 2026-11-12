"""Comparison directory ownership, including conservative adoption of old exports."""

import hashlib
import random
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict

from .attempts import history
from .export_directory import bind_directory, read_exact


def check_legacy(journal, folder, manifest):
    cells = list(manifest["cells"])
    cell_ids = {c["cell_id"] for c in cells}
    private = folder / "private"
    for entry in private.iterdir():
        if entry.is_dir() and (entry.is_symlink() or entry.name not in cell_ids | {"sources"}):
            raise Conflict("audio_legacy_export_contains_foreign_private_records")
    sources = private / "sources"
    expected = {
        str(RuntimeRef.model_validate(ref).record_id) + ".json": ref
        for ref in manifest["source_refs"].values()
    }
    if sources.exists():
        for entry in sources.iterdir():
            if entry.name not in expected or not entry.is_file() or entry.is_symlink():
                raise Conflict("audio_legacy_export_contains_foreign_sources")
            source = journal.base.read(RuntimeRef.model_validate(expected[entry.name]))
            if not read_exact(entry, source):
                raise Conflict("audio_legacy_export_source_content_mismatch")
    random.Random(manifest["configuration"]["blind_seed"]).shuffle(cells)
    labels = {f"S{n:03d}.wav": cell for n, cell in enumerate(cells, 1)}
    for entry in (folder / "blind").glob("S*.wav"):
        if entry.name not in labels or not entry.is_file() or entry.is_symlink():
            raise Conflict("audio_legacy_export_contains_foreign_samples")
        known = {
            revision["result"]["blind_audio_blob"]["sha256"]
            for row in history(journal, labels[entry.name])
            for revision in row["processing"]
            if revision["result"]
        }
        if hashlib.sha256(entry.read_bytes()).hexdigest() not in known:
            raise Conflict("audio_legacy_export_sample_content_mismatch")


def bind_comparison(journal, folder, manifest):
    folder = Path(folder)
    bind_directory(
        folder,
        "export-binding.json",
        dict(
            contract="audio-comparison-export-binding/1",
            run_id=str(journal.run_id),
            manifest_ref=journal.ref("manifest").model_dump(mode="json"),
            manifest_hash=digest(manifest),
        ),
        manifest,
        legacy_validator=lambda: check_legacy(journal, folder, manifest),
    )
