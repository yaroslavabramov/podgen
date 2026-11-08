"""Compile ordinary and dedicated repair wires without a provider or token endpoint."""

from podgen.domain.errors import ContractError
from podgen.writing.craft_reference import AUTHOR_STAGES, load_reference, pinned_reference
from podgen.writing.model_references import preview_references
from podgen.writing.plan_views import candidate_view
from podgen.writing.planning_versions import planning_snapshot
from podgen.writing.projection_versions import CURRENT, manifest_version
from podgen.writing.projections import CONTRACT, validate_model_content
from podgen.writing.prompts import (
    load_anchor_repair_instructions,
    load_instruction_set,
    load_plan_target_repair_instructions,
)
from podgen.writing.reader_schema import VERSION as READER_SCHEMA_VERSION
from podgen.writing.revision.contracts import FACT_VERSION

from .compiler import response_request
from .repair_projection import anchor_payload, target_payload
from .role_request import compile_role_request
from .role_schema import response_schema
from .workflow import prompt_stage


def preview_request(run_id, stage, profile, materials, *, snapshot=None, **inputs):
    from .planning_continuation.preview import validate as validate_continuation_preview
    from .preview_inputs import validate_preview_inputs, validate_preview_snapshots

    if snapshot:
        from podgen.writing.author_reply import verify_pin

        verify_pin(snapshot.manifest, materials.speaker_labels)
    validate_preview_inputs(stage, fixed=materials.fixed is not None, inputs=inputs)
    validate_preview_snapshots(stage, materials, inputs)
    validate_continuation_preview(stage, snapshot, inputs)
    inputs = {key: value for key, value in inputs.items() if value is not None}
    projection = manifest_version(snapshot.manifest) if snapshot else CURRENT
    from podgen.writing.model_references import run_references

    references = run_references(materials, snapshot.manifest) if snapshot else None
    if stage not in AUTHOR_STAGES:
        reference = None
    elif snapshot:
        reference = pinned_reference(snapshot.manifest)
    else:
        current = load_reference(materials.output_language)
        reference = current["text"] if current is not None else None
    pinned_instructions = snapshot.instructions if snapshot else None
    reader_version = (
        snapshot.manifest.get("reader_review_schema_version") if snapshot else READER_SCHEMA_VERSION
    )
    fact_version = snapshot.manifest.get("fact_review_schema_version") if snapshot else FACT_VERSION

    def schema():
        if snapshot and stage in snapshot.manifest["response_schemas"]:
            return snapshot.manifest["response_schemas"][stage]
        return response_schema(
            stage,
            CONTRACT,
            projection_version=projection,
            reader_schema_version=reader_version,
            fact_schema_version=fact_version,
        )

    from .reading_workflow import ALL_READING_STAGES, base_stage

    if stage in ALL_READING_STAGES:
        from podgen.writing.prompts import load_reading_instructions

        from .reading_cycle import combine
        from .role_request import compile_reading_request

        base = base_stage(stage)
        instructions = pinned_instructions
        if instructions is None:
            instructions = load_reading_instructions("dual-revise" if base == "revise" else base).body
        readings = None
        revision = None
        if base == "revise":
            readings = dict(reader=inputs["reader_feedback"], fact=inputs["fact_feedback"])
            from .revision_preview import prepare as prepare_revision

            revision = prepare_revision(
                stage,
                snapshot,
                script=inputs["draft"],
                readings=readings,
                composition=inputs.get("composition"),
            )
        return compile_reading_request(
            run_id,
            stage,
            profile,
            materials,
            instructions,
            revision.schema if revision else schema(),
            projection_version=projection,
            reference_catalog=references,
            craft_reference=reference,
            script=inputs["draft"],
            readings=readings,
            decision=combine(readings) if readings else None,
            approved_plan=inputs.get("approved_plan"),
            **(revision.inputs if revision else {}),
        )
    if stage not in ("anchor-repair", "plan-target-repair"):
        instructions = pinned_instructions
        if instructions is None:
            instructions = load_instruction_set(materials.format, include_planning=True)[
                prompt_stage(stage)
            ].body
        return compile_role_request(
            run_id,
            stage,
            profile,
            materials,
            instructions,
            schema(),
            projection_version=projection,
            reference_catalog=references,
            craft_reference=reference,
            **inputs,
        )
    if stage == "anchor-repair":
        from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
        from podgen.writing.fact_review import FactFeedback
        from podgen.writing.roles import EditorialFeedback, WriteOutput

        feedback_model = (
            FactFeedback
            if any("support_labels" in row for row in inputs["feedback"]["findings"])
            else EditorialFeedback
        )
        if "revision_scope" in inputs["feedback"]:
            from podgen.writing.reader_schema import snapshot_model

            feedback_model = snapshot_model(inputs["feedback"])
        plan = AnchorRepairPlan.build(
            WriteOutput.model_validate(inputs["draft"]), feedback_model.model_validate(inputs["feedback"])
        )
        if not plan.broken:
            raise ContractError("review_anchor_repair_not_eligible")
        payload = anchor_payload(plan)
        instructions = load_anchor_repair_instructions().body
        model = AnchorChoices
        role_stage = "fact-review" if feedback_model == FactFeedback else "review"
        name = "anchor_choices"
    else:
        from podgen.writing.plan_targets import PlanTargetChoices, PlanTargetRepair
        from podgen.writing.planning_roles import PlanReviewOutput

        plan = PlanTargetRepair.build(
            planning_snapshot(candidate_view(inputs["candidate"], projection_version=CURRENT)),
            PlanReviewOutput.model_validate(inputs["plan_review"]),
            materials.material_catalog,
        )
        if not plan.invalid:
            raise ContractError("plan_target_repair_not_eligible")
        payload = target_payload(plan)
        instructions = load_plan_target_repair_instructions().body
        model = PlanTargetChoices
        role_stage = "plan-review"
        name = "plan_target_choices"
    references = preview_references(materials, CURRENT)
    if references is not None:
        payload = references.translate(payload)
    schema = model.model_json_schema()
    validate_model_content(dict(instructions=instructions, input=payload, schema=schema), materials.documents)
    body = response_request(run_id, role_stage, profile, payload, instructions, schema)
    body["text"]["format"]["name"] = name
    return body
