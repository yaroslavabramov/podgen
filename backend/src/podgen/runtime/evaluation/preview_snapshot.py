"""Read an existing run's immutable preview dependencies without reloading current resources."""

from dataclasses import dataclass

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import Conflict

from .compiler import materials_hash


@dataclass(frozen=True)
class PreviewSnapshot:
    manifest: dict
    instructions: str | None
    script: dict | None = None
    composition: dict | None = None
    planning_inputs: dict | None = None
    revision_comparison: dict | None = None
    fact_plan: dict | None = None
    continuation_inputs: dict | None = None


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
    from .reading_workflow import base_stage
    from .script_revision import enabled

    script, composition = None, None
    if base_stage(stage) == "revise" and enabled(manifest):
        invocation = journal.invocation(run_id, stage)
        if invocation:
            record = journal.read(
                RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
            )
            source = record["context"]["script_ref"]
        else:
            number = int(stage.rsplit("-", 1)[1])
            source = manifest["command"].get("initial_script_ref") if number == 1 else None
            previous = journal.invocation(run_id, "draft" if number == 1 else f"revise-{number - 1}")
            if source is None and previous and previous["status"] == "completed":
                source = RuntimeRef(
                    record_kind="tool_execution", record_id=previous["output_record_id"]
                ).model_dump(mode="json")
        if source is not None:
            from .revision_invocation import source_state

            script = journal.read(RuntimeRef.model_validate(source))["output"]
            composition = source_state(journal, source, script)
    from podgen.writing.revision import verification

    from . import recheck_source
    from .planning_continuation.preview import attach

    comparison = None
    if verification.applies(stage, manifest.get(verification.KEY)):
        script, comparison = recheck_source.load(journal, run_id, stage, version=manifest[verification.KEY])

    from podgen.writing.planning_choice import KEY, SCENE_VERSION

    fact_plan = None
    if base_stage(stage) == "fact-review" and manifest.get(KEY) == SCENE_VERSION:
        from .planning import verify_clearance

        if manifest.get("editorial_continuation"):
            from .editorial_continuation.source import own_plan

            fact_plan = own_plan(journal, manifest)["accepted_plan"]
        else:
            proof = verify_clearance(journal, run_id)
            fact_plan = journal.read(RuntimeRef.model_validate(proof["candidate_plan_ref"]))["output"]
    snapshot = PreviewSnapshot(
        manifest, instructions, script, composition, fact_plan=fact_plan, revision_comparison=comparison
    )
    from .editorial_continuation.preview import attach as attach_editorial

    return attach_editorial(attach(snapshot, journal, run_id, stage, materials), journal, run_id, stage)
