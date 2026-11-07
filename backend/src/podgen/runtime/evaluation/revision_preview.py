"""Preview uses the same mode, target catalog and private schema as the normal invocation."""

from dataclasses import dataclass

from podgen.domain.errors import Conflict, ContractError
from podgen.writing.revision.contracts import VERSION

from . import script_revision


@dataclass(frozen=True)
class RevisionPreview:
    schema: dict
    inputs: dict


def prepare(stage, snapshot, *, script, readings, composition=None):
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
        manifest = dict(script_revision_contract=VERSION, revision_response_schemas=script_revision.schemas())
    selection = script_revision.operation(script, readings, int(stage.rsplit("-", 1)[1]) - 1)
    return RevisionPreview(
        script_revision.bound_schema(manifest, selection),
        dict(revision_operation=script_revision.model_operation(selection), composition=composition),
    )
