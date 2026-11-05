"""One separately archived planning call, bound to an exact candidate and complete allowed sources."""

from uuid import uuid5

from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError

from .compiler import response_request
from .contracts import RunFailure
from .plan_target_recovery import apply_plan_target_repair
from .profiles import load_profile
from .stage import StageResult
from .usage import prior_attempt_usage
from .validation import load_unique_json
from .workflow import output_model, profile_for


def execute_planning_stage(
    stage,
    *,
    journal,
    gateway,
    generation,
    command,
    manifest,
    materials,
    totals,
    guard,
    candidate=None,
    candidate_ref=None,
    review=None,
    review_ref=None,
):
    profile_ref = profile_for(command, stage)
    profile = load_profile(generation, command.project_id, profile_ref, stage)
    payload = dict(
        contract="own-plan-evaluation-v1",
        stage=stage,
        plan_mode="own-plan",
        author_materials=materials.as_payload(),
        output_language=manifest["output_language"],
    )
    context = dict(
        manifest_ref=RuntimeRef(
            record_kind="context_assembly", record_id=uuid5(command.run_id, "manifest")
        ).model_dump(mode="json"),
        profile_ref=profile_ref.model_dump(mode="json"),
        prompt_ref=manifest["prompt_refs"][stage],
        materials_hash=manifest["materials_hash"],
        candidate_plan_ref=candidate_ref,
        candidate_plan_hash=digest(candidate) if candidate is not None else None,
        plan_review_ref=review_ref,
    )
    if candidate is not None:
        payload.update(
            candidate_plan=candidate, candidate_plan_ref=candidate_ref, candidate_plan_hash=digest(candidate)
        )
    if (
        candidate is not None
        and stage.startswith("plan-review")
        and manifest.get("plan_review_target_catalog") == "planning-output-pointers-v1"
    ):
        from podgen.writing.plan_targets import allowed_review_targets

        payload.update(
            review_target_root="PlanningOutput inside candidate_plan; omit the enclosing /candidate_plan prefix.",
            allowed_review_targets=allowed_review_targets(candidate),
        )
    if review is not None:
        payload.update(plan_review=review, plan_review_ref=review_ref)
    invocation = journal.invocation(command.run_id, stage)
    if invocation is None:
        prompt = generation.get(
            command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"][stage])
        )
        body = response_request(
            command.run_id,
            stage,
            profile,
            payload,
            prompt.payload["template"],
            manifest["response_schemas"][stage],
        )
        invocation = journal.prepare(command.run_id, stage, body, context)
    saved_request = journal.read(
        RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
    )
    if saved_request["context"] != context:
        raise ContractError("planning_request_binding_mismatch")
    prior = prior_attempt_usage(journal, invocation, profile)
    charged = {key: totals[key] + prior[key] for key in ("input", "output")}
    if invocation["status"] != "completed":
        raw = gateway.invoke(invocation, profile, limits=command.limits, totals=charged, guard=guard)
        repair_choices = None
        try:
            load_unique_json(raw)
            if invocation["operation_kind"] == "plan_target_repair":
                parsed, repair_choices = apply_plan_target_repair(
                    journal, invocation, raw, candidate=candidate, candidate_ref=candidate_ref
                )
            else:
                parsed = output_model(stage).model_validate_json(raw)
            validate_planning(
                parsed, stage, materials.as_payload()["material_catalog"], candidate, materials.speaker_labels
            )
        except (ValidationError, ValueError, ContractError) as error:
            journal.event(
                invocation,
                "output_validation_error",
                data={"code": getattr(error, "code", "planning_output_validation_failed")},
            )
            journal.transition(invocation, "validation_failed")
            raise RunFailure(
                getattr(error, "code", "planning_output_validation_failed"),
                run_id=command.run_id,
                invocation_id=invocation["invocation_id"],
            ) from None
        output = parsed.model_dump(mode="json")
        record = dict(
            contract="writing-evaluation-output-v1",
            run_id=str(command.run_id),
            stage=stage,
            invocation_id=str(invocation["invocation_id"]),
            request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=invocation["request_record_id"]
            ).model_dump(mode="json"),
            output=output,
            repair_choices=repair_choices,
            candidate_plan_ref=candidate_ref,
            candidate_plan_hash=digest(candidate) if candidate is not None else None,
            plan_review_ref=review_ref,
            candidate_id=str(uuid5(invocation["invocation_id"], "plan-candidate"))
            if stage in ("plan", "plan-revise")
            else None,
            semantic_acceptance=False,
            production_ready=False,
        )
        output_ref = journal.record(record, record_id=uuid5(invocation["invocation_id"], "output"))
        invocation = journal.transition(invocation, "completed", output_ref=output_ref)
    else:
        record = journal.output(invocation)
        if (
            record.get("candidate_plan_ref") != candidate_ref
            or record.get("candidate_plan_hash") != (digest(candidate) if candidate is not None else None)
            or record.get("plan_review_ref") != review_ref
        ):
            raise ContractError("planning_output_binding_mismatch")
    return StageResult(
        journal.output(invocation)["output"],
        RuntimeRef(record_kind="tool_execution", record_id=invocation["output_record_id"]),
        (invocation["input_tokens"] or 0) + prior["input"],
        (invocation["output_tokens"] if invocation["output_tokens"] is not None else profile.output_limit)
        + prior["output"],
    )


def validate_planning(parsed, stage, catalog, candidate, speakers):
    if stage.startswith("plan-review"):
        parsed.validate_against(output_model("plan").model_validate(candidate), material_locators=catalog)
    else:
        parsed.validate_against(material_locators=catalog, speaker_ids=speakers)
