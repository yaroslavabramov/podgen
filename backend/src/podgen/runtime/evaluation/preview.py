"""Compile ordinary and dedicated repair wires without a provider or token endpoint."""

from podgen.domain.errors import ContractError
from podgen.writing import author_plan, ending_guidance, fact_context, prompt_cache, source_passages
from podgen.writing.behavior import resolve_behavior
from podgen.writing.craft_reference import AUTHOR_STAGES, pinned_reference
from podgen.writing.model_references import preview_references
from podgen.writing.plan_review_descriptions import schema_version_from_manifest
from podgen.writing.plan_selection import from_manifest as selection_from_manifest
from podgen.writing.plan_views import candidate_view
from podgen.writing.planning_choice import from_manifest as planning_from_manifest
from podgen.writing.planning_duration import from_manifest as duration_from_manifest
from podgen.writing.planning_versions import planning_snapshot
from podgen.writing.projection_versions import manifest_version
from podgen.writing.projections import CONTRACT, validate_model_content
from podgen.writing.reading_views import reading_audience
from podgen.writing.revision import verification
from podgen.writing.revision.context import from_manifest as revision_context_from_manifest

from . import source_tool_contract
from .compiler import response_request
from .repair_projection import anchor_payload, target_payload
from .role_request import compile_role_request
from .role_schema import response_schema
from .workflow import prompt_stage


def preview_request(
    run_id, stage, profile, materials, *, snapshot=None, behavior_profile=None, editorial_pass=None, **inputs
):
    from .planning_continuation.preview import validate as validate_continuation_preview
    from .preview_inputs import validate_preview_inputs, validate_preview_snapshots

    if snapshot and (
        behavior_profile is not None
        and behavior_profile != snapshot.manifest.get("behavior_profile")
        or editorial_pass is not None
        and editorial_pass != snapshot.manifest.get("editorial_pass", 1)
    ):
        raise ContractError("preview_behavior_profile_mismatch")
    behavior = None if snapshot else resolve_behavior(behavior_profile, editorial_pass or 1)
    tools = (
        source_tool_contract.from_manifest(snapshot.manifest)
        if snapshot
        else source_tool_contract.create(behavior)
    )
    if behavior:
        behavior.validate_model(stage, profile)
    projection = manifest_version(snapshot.manifest) if snapshot else behavior.projection
    if snapshot:
        from podgen.writing.author_reply import verify_pin

        verify_pin(snapshot.manifest, materials.speaker_labels)
    if snapshot and snapshot.fact_plan is not None:
        if inputs.get("approved_plan") not in (None, snapshot.fact_plan):
            raise ContractError("preview_claim_scope_plan_mismatch")
        inputs["approved_plan"] = snapshot.fact_plan
    validate_preview_inputs(stage, fixed=materials.fixed is not None, inputs=inputs)
    verification_pin = snapshot.manifest.get(verification.KEY) if snapshot else behavior.verification
    validate_preview_snapshots(
        stage,
        materials,
        inputs,
        projection_version=projection,
        revision_verification_contract=verification_pin,
    )
    validate_continuation_preview(stage, snapshot, inputs)
    inputs = {key: value for key, value in inputs.items() if value is not None}
    plan_review_version = (
        schema_version_from_manifest(snapshot.manifest) if snapshot else behavior.plan_review_schema
    )
    handoff = author_plan.from_manifest(snapshot.manifest) if snapshot else behavior.author_plan_handoff
    cache_layout = prompt_cache.from_manifest(snapshot.manifest) if snapshot else behavior.prompt_cache_layout
    ending = (
        ending_guidance.from_manifest(snapshot.manifest) if snapshot else behavior.planning_ending_guidance
    )
    excerpts = (
        source_passages.from_manifest(snapshot.manifest) if snapshot else behavior.author_source_excerpts
    )
    planning_contract = planning_from_manifest(snapshot.manifest) if snapshot else behavior.planning_contract
    planning_duration = duration_from_manifest(snapshot.manifest) if snapshot else behavior.planning_duration
    selection = selection_from_manifest(snapshot.manifest) if snapshot else behavior.selection
    from podgen.writing.model_references import run_references

    references = run_references(materials, snapshot.manifest) if snapshot else None
    revision_context = (
        revision_context_from_manifest(snapshot.manifest) if snapshot else behavior.revision_context
    )
    if stage not in AUTHOR_STAGES or (stage.startswith("revise-") and revision_context is not None):
        reference = None
    elif snapshot:
        reference = pinned_reference(snapshot.manifest)
    else:
        current = behavior.craft(materials.output_language)
        reference = current["text"] if current is not None else None
    pinned_instructions = snapshot.instructions if snapshot else None
    reader_version = (
        snapshot.manifest.get("reader_review_schema_version") if snapshot else behavior.reader_schema
    )
    fact_version = snapshot.manifest.get("fact_review_schema_version") if snapshot else behavior.fact_schema

    def schema():
        if snapshot and stage in snapshot.manifest["response_schemas"]:
            return snapshot.manifest["response_schemas"][stage]
        return response_schema(
            stage,
            CONTRACT,
            projection_version=projection,
            planning_output_contract=planning_contract,
            plan_review_schema_version=plan_review_version,
            planning_ending_guidance=ending,
            reader_schema_version=reader_version,
            **(
                {"revision_verification_contract": behavior.verification}
                if behavior and behavior.verification
                else {}
            ),
            fact_schema_version=fact_version,
        )

    from .reading_workflow import ALL_READING_STAGES, base_stage

    if stage in ALL_READING_STAGES:
        from .reading_cycle import combine
        from .role_request import compile_reading_request

        base = base_stage(stage)
        recheck = None
        if verification.applies(stage, verification_pin):
            if snapshot is None:
                raise ContractError("preview_revision_verification_requires_saved_rewrite")
            if snapshot.script != inputs["draft"] or snapshot.revision_comparison is None:
                raise ContractError("preview_revision_verification_source_mismatch")
            recheck = snapshot.revision_comparison
        instructions = pinned_instructions
        if instructions is None:
            if snapshot:
                raise ContractError("preview_stage_not_pinned")
            role = "dual-revise" if base == "revise" else base
            if stage != base and base == "reader-review" and behavior.verification:
                role = "revision-verification"
            instructions = behavior.instructions(role, materials.format).body
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
                prompt_cache_layout=cache_layout,
                revision_context_contract=revision_context,
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
            source_tool_contract=tools,
            prompt_cache_layout=cache_layout,
            fact_context_contract=fact_context.from_manifest(snapshot.manifest)
            if snapshot
            else behavior.fact_context,
            plan_selection_contract=selection,
            author_plan_handoff=handoff,
            author_source_excerpts=excerpts,
            craft_reference=reference,
            revision_context_contract=revision_context,
            revision_comparison=recheck,
            audience=reading_audience(materials.packet.assignment, snapshot.manifest)
            if snapshot
            else behavior.audience(materials.packet.assignment),
            script=inputs["draft"],
            readings=readings,
            decision=combine(readings) if readings else None,
            approved_plan=inputs.get("approved_plan"),
            **(revision.inputs if revision else {}),
        )
    if stage not in ("anchor-repair", "plan-target-repair"):
        instructions = pinned_instructions
        if instructions is None:
            if snapshot:
                raise ContractError("preview_stage_not_pinned")
            instructions = behavior.instructions(prompt_stage(stage), materials.format).body
        return compile_role_request(
            run_id,
            stage,
            profile,
            materials,
            instructions,
            schema(),
            projection_version=projection,
            reference_catalog=references,
            source_tool_contract=tools,
            prompt_cache_layout=cache_layout,
            plan_selection_contract=selection,
            author_plan_handoff=handoff,
            author_source_excerpts=excerpts,
            planning_duration_contract=planning_duration,
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

            feedback_model = snapshot_model(
                inputs["feedback"], revision_verification_contract=verification_pin
            )
        plan = AnchorRepairPlan.build(
            WriteOutput.model_validate(inputs["draft"]), feedback_model.model_validate(inputs["feedback"])
        )
        if not plan.broken:
            raise ContractError("review_anchor_repair_not_eligible")
        payload = anchor_payload(plan)
        instructions = (
            snapshot.manifest["anchor_repair_instructions"]["body"]
            if snapshot
            else behavior.instructions("anchor-repair", materials.format).body
        )
        model = AnchorChoices
        role_stage = "fact-review" if feedback_model == FactFeedback else "review"
        name = "anchor_choices"
    else:
        from podgen.writing.plan_targets import PlanTargetChoices, PlanTargetRepair
        from podgen.writing.planning_roles import PlanReviewOutput

        plan = PlanTargetRepair.build(
            planning_snapshot(candidate_view(inputs["candidate"], projection_version=projection)),
            PlanReviewOutput.model_validate(inputs["plan_review"]),
            materials.material_catalog,
        )
        if not plan.invalid:
            raise ContractError("plan_target_repair_not_eligible")
        payload = target_payload(plan)
        instructions = (
            snapshot.manifest["plan_target_repair_instructions"]["body"]
            if snapshot
            else behavior.instructions("plan-target-repair", materials.format).body
        )
        model = PlanTargetChoices
        role_stage = "plan-review"
        name = "plan_target_choices"
    if snapshot is None:
        references = preview_references(materials, projection)
    if references is not None:
        payload = references.translate(payload)
    schema = model.model_json_schema()
    validate_model_content(dict(instructions=instructions, input=payload, schema=schema), materials.documents)
    body = response_request(
        run_id, role_stage, profile, payload, instructions, schema, prompt_cache_layout=cache_layout
    )
    body["text"]["format"]["name"] = name
    return body
