"""Preview uses the same mode, target catalog and private schema as the normal invocation."""

from dataclasses import dataclass

from podgen.domain.errors import Conflict, ContractError
from podgen.writing import prompt_cache
from podgen.writing.projection_versions import V9
from podgen.writing.revision.context import from_manifest as revision_context_from_manifest
from podgen.writing.revision.contracts import VERSION

from . import script_revision


@dataclass(frozen=True)
class RevisionPreview:
    schema: dict
    inputs: dict


def prepare(
    stage,
    snapshot,
    *,
    script,
    readings,
    composition=None,
    prompt_cache_layout=None,
    revision_context_contract=None,
):
    if snapshot is not None:
        if not script_revision.enabled(snapshot.manifest):
            if composition is not None:
                raise ContractError("legacy_revision_has_no_composition_input")
            return None
        manifest = snapshot.manifest
        if snapshot.script is not None:
            if snapshot.script != script:
                raise Conflict("preview_revision_source_mismatch")
            if composition is not None and composition != snapshot.composition:
                raise Conflict("preview_composition_source_mismatch")
            composition = snapshot.composition
    else:
        if any("revision_scope" not in review for review in readings.values()):
            if composition is not None:
                raise ContractError("revision_scope_required")
            return None
        manifest = dict(
            script_revision_contract=VERSION,
            revision_response_schemas=script_revision.schemas(prompt_cache_layout=prompt_cache_layout),
        )
        if prompt_cache_layout is not None:
            manifest.update(
                behavior_profile="production/1",
                role_projection_version=V9,
                **{prompt_cache.KEY: prompt_cache_layout},
            )
    context = revision_context_from_manifest(manifest) if snapshot else revision_context_contract
    selection = script_revision.operation(
        script, readings, int(stage.rsplit("-", 1)[1]) - 1, revision_context_contract=context
    )
    return RevisionPreview(
        script_revision.bound_schema(manifest, selection),
        dict(revision_operation=script_revision.model_operation(selection), composition=composition),
    )
