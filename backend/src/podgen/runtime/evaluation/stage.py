"""One whole-episode role: compile pinned inputs, invoke, validate and commit its complete output."""

from dataclasses import dataclass
from uuid import uuid5

from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.craft_reference import pinned_reference
from podgen.writing.model_references import CATALOG_KEY, run_references
from podgen.writing.projection_versions import manifest_version, script_handoff

from .compiler import COMPILER_VERSION, compile_request
from .contracts import RunFailure
from .profiles import load_profile
from .review_repair import apply_anchor_repair, prepare_anchor_repair
from .scripts import script_view
from .usage import prior_attempt_usage
from .validation import RoleOutputError, validate_output


@dataclass(frozen=True)
class StageResult:
    output: dict
    ref: RuntimeRef
    input_tokens: int
    output_tokens_or_reservation: int


def execute_stage(
    stage,
    *,
    journal,
    gateway,
    generation,
    command,
    manifest,
    materials,
    outputs,
    records,
    totals,
    guard,
    repair_review_anchors=False,
    own_plan=None,
):
    references = (
        run_references(materials, manifest) if command.input_contract == "editorial-role-packets/1" else None
    )
    profile_ref = command.reviewer_profile_ref if stage == "review" else command.writer_profile_ref
    profile = load_profile(generation, command.project_id, profile_ref, stage)
    prompt_ref = ArtifactRef.model_validate(manifest["prompt_refs"][stage])
    instructions = generation.get(command.project_id, prompt_ref).payload["template"]
    if command.input_contract == "editorial-role-packets/1":
        from .role_request import compile_role_request

        body = compile_role_request(
            command.run_id,
            stage,
            profile,
            materials,
            instructions,
            manifest["response_schemas"][stage],
            projection_version=manifest_version(manifest),
            reference_catalog=references,
            craft_reference=pinned_reference(manifest),
            approved_plan=own_plan["accepted_plan"] if own_plan else None,
            draft=outputs.get("draft"),
            feedback=outputs.get("review"),
        )
    else:
        body = compile_request(
            command.run_id,
            stage,
            profile,
            materials.as_payload(),
            instructions,
            manifest["response_schemas"][stage],
            output_language=manifest.get("output_language"),
            draft=outputs.get("draft"),
            feedback=outputs.get("review"),
            own_plan=own_plan,
        )
    context = dict(
        manifest_ref=RuntimeRef(
            record_kind="context_assembly", record_id=uuid5(command.run_id, "manifest")
        ).model_dump(mode="json"),
        profile_ref=profile_ref.model_dump(mode="json"),
        prompt_ref=prompt_ref.model_dump(mode="json"),
        materials_hash=manifest["materials_hash"],
        compiler_version=COMPILER_VERSION,
        draft_ref=records.get("draft"),
        feedback_ref=records.get("review") if stage == "revise" else None,
    )
    if command.input_contract == "editorial-role-packets/1":
        context["role_input_contract"] = command.input_contract
        if "role_projection_version" in manifest:
            context["role_projection_version"] = manifest["role_projection_version"]
    if own_plan is not None:
        context.update(
            accepted_plan_ref=own_plan["accepted_plan_ref"],
            planning_clearance_ref=own_plan["planning_clearance_ref"],
        )
    if references is not None:
        context[CATALOG_KEY] = references.as_payload()
    if command.input_contract == "editorial-role-packets/1" and script_handoff(manifest_version(manifest)):
        selected = (
            own_plan["accepted_plan"]
            if own_plan
            else materials.fixed.plan.model_dump(mode="json")
            if materials.fixed
            else None
        )
        context["accepted_plan_hash"] = digest(selected) if selected is not None else None
    invocation = journal.invocation(command.run_id, stage)
    if invocation is None:
        invocation = journal.prepare(command.run_id, stage, body, context)
    if command.input_contract == "editorial-role-packets/1" and script_handoff(manifest_version(manifest)):
        saved = journal.read(
            RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
        )
        if any(saved["context"].get(key) != value for key, value in context.items()):
            raise ContractError("writing_request_binding_mismatch")
    if (
        stage == "review"
        and invocation["operation_kind"] == "role"
        and invocation["status"] == "validation_failed"
        and repair_review_anchors
    ):
        invocation = prepare_anchor_repair(
            journal, invocation, draft=outputs["draft"], draft_ref=records["draft"], guard=guard
        )
    while True:
        prior = prior_attempt_usage(journal, invocation, profile)
        charged = dict(input=totals["input"] + prior["input"], output=totals["output"] + prior["output"])
        if invocation["status"] == "completed":
            record = journal.output(invocation)
            output = record["output"]
            break
        raw = gateway.invoke(invocation, profile, limits=command.limits, totals=charged, guard=guard)
        repair_choices = None
        try:
            if invocation["operation_kind"] == "anchor_repair":
                output, repair_choices = apply_anchor_repair(journal, invocation, raw, draft=outputs["draft"])
            else:
                output = validate_output(
                    stage, raw, speaker_labels=materials.speaker_labels, draft=outputs.get("draft")
                )
        except (RoleOutputError, ValidationError, ContractError, RunFailure) as error:
            details = (
                error.details
                if isinstance(error, RoleOutputError)
                else [{"code": getattr(error, "code", "invalid_anchor_choices")}]
            )
            journal.event(invocation, "output_validation_error", data={"details": details})
            journal.transition(invocation, "validation_failed")
            if stage == "review" and invocation["operation_kind"] == "role" and repair_review_anchors:
                invocation = prepare_anchor_repair(
                    journal,
                    journal.invocation(command.run_id, stage, invocation["attempt_index"]),
                    draft=outputs["draft"],
                    draft_ref=records["draft"],
                    guard=guard,
                )
                continue
            raise RunFailure(
                getattr(error, "code", "role_output_validation_failed"),
                run_id=command.run_id,
                invocation_id=invocation["invocation_id"],
            ) from None
        break
    if invocation["status"] != "completed":
        previous = journal.output(journal.invocation(command.run_id, "draft")) if stage == "revise" else None
        record = dict(
            contract="writing-evaluation-output-v1",
            run_id=str(command.run_id),
            stage=stage,
            invocation_id=str(invocation["invocation_id"]),
            request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=invocation["request_record_id"]
            ).model_dump(mode="json"),
            output=output,
            attempt_index=invocation["attempt_index"],
            parent_invocation_id=str(invocation["parent_invocation_id"])
            if invocation["parent_invocation_id"]
            else None,
            repair_choices=repair_choices,
            parent_script_ref=records.get("draft") if stage == "revise" else None,
            spoken_view=script_view(invocation["invocation_id"], output, previous)
            if stage != "review"
            else [],
            accepted_plan_ref=own_plan["accepted_plan_ref"] if own_plan is not None else None,
            semantic_acceptance=False,
            production_ready=False,
        )
        output_ref = journal.record(record, record_id=uuid5(invocation["invocation_id"], "output"))
        journal.transition(invocation, "completed", output_ref=output_ref)
    saved = journal.invocation(command.run_id, stage)
    return StageResult(
        output,
        RuntimeRef(record_kind="tool_execution", record_id=saved["output_record_id"]),
        (saved["input_tokens"] or 0) + prior["input"],
        (saved["output_tokens"] if saved["output_tokens"] is not None else profile.output_limit)
        + prior["output"],
    )
