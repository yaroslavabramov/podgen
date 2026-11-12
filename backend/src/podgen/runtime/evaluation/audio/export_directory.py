"""Verify ownership before writing anything into an audio export directory."""

import json
from pathlib import Path

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_exact(path, expected):
    try:
        return digest(json.loads(path.read_text())) == digest(expected)
    except (OSError, ValueError, TypeError):
        return False


def bind_directory(folder, filename, binding, manifest, *, legacy_validator=None):
    """A matching old manifest can establish ownership; unknown nonempty folders cannot."""
    folder = Path(folder)
    binding_path = folder / filename
    manifest_path = folder / "private" / "manifest.json"
    if folder.exists() and not folder.is_dir():
        raise Conflict("audio_export_path_is_not_directory")
    if binding_path.exists():
        if not read_exact(binding_path, binding):
            raise Conflict("audio_export_directory_bound_to_other_manifest")
        if manifest_path.exists() and not read_exact(manifest_path, manifest):
            raise Conflict("audio_export_saved_manifest_mismatch")
        return
    if folder.exists() and any(folder.iterdir()):
        if not manifest_path.is_file() or not read_exact(manifest_path, manifest):
            raise Conflict("audio_export_nonempty_directory_not_verified")
        if legacy_validator is not None:
            legacy_validator()
    folder.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also rejects another run claiming the directory concurrently.
    try:
        with binding_path.open("x") as output:
            output.write(json.dumps(binding, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        if not read_exact(binding_path, binding):
            raise Conflict("audio_export_directory_bound_to_other_manifest") from None
