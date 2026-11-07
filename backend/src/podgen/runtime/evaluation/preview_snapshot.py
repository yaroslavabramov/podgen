"""Read an existing run's immutable preview dependencies without reloading current resources."""

from dataclasses import dataclass

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import Conflict

from .compiler import materials_hash


@dataclass(frozen=True)
class PreviewSnapshot:
    manifest: dict
    instructions: str | None
    planning_inputs: dict | None = None


def load_preview_snapshot(journal, generation, run_id, stage, materials):
    if journal.run(run_id) is None:
        return None
    manifest = journal.manifest(run_id)
    if materials_hash(materials.as_payload()) != manifest["materials_hash"]:
        raise Conflict("preview_frozen_materials_mismatch")
    prompt = manifest["prompt_refs"].get(stage)
    instructions = (
        generation.get(journal.project, ArtifactRef.model_validate(prompt)).payload["template"]
        if prompt is not None
        else None
    )
    from .planning_continuation.preview import attach

    return attach(PreviewSnapshot(manifest, instructions), journal, run_id, stage, materials)
