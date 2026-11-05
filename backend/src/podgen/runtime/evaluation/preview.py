"""Compile ordinary and dedicated repair wires without a provider or token endpoint."""

from podgen.domain.errors import ContractError
from podgen.writing.projections import CONTRACT, validate_model_content
from podgen.writing.prompts import (
    load_anchor_repair_instructions,
    load_instruction_set,
    load_plan_target_repair_instructions,
)

from .compiler import response_request
from .repair_projection import anchor_payload, target_payload
from .role_request import compile_role_request
from .role_schema import response_schema
from .workflow import prompt_stage


def preview_request(run_id, stage, profile, materials, **inputs):
    from .preview_inputs import validate_preview_inputs, validate_preview_snapshots

    validate_preview_inputs(stage, fixed=materials.fixed is not None, inputs=inputs)
    validate_preview_snapshots(stage, materials, inputs)
    if stage not in ("anchor-repair", "plan-target-repair"):
        prompt = load_instruction_set(materials.format, include_planning=True)[prompt_stage(stage)]
        return compile_role_request(
            run_id, stage, profile, materials, prompt.body, response_schema(stage, CONTRACT), **inputs
        )
    if stage == "anchor-repair":
        from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
        from podgen.writing.roles import EditorialFeedback, WriteOutput

        plan = AnchorRepairPlan.build(
            WriteOutput.model_validate(inputs["draft"]), EditorialFeedback.model_validate(inputs["feedback"])
        )
        if not plan.broken:
            raise ContractError("review_anchor_repair_not_eligible")
        payload = anchor_payload(plan)
        instructions = load_anchor_repair_instructions().body
        model = AnchorChoices
        role_stage = "review"
        name = "anchor_choices"
    else:
        from podgen.writing.plan_targets import PlanTargetChoices, PlanTargetRepair
        from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput

        plan = PlanTargetRepair.build(
            PlanningOutput.model_validate(inputs["candidate"]),
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
    schema = model.model_json_schema()
    validate_model_content(dict(instructions=instructions, input=payload, schema=schema), materials.documents)
    body = response_request(run_id, role_stage, profile, payload, instructions, schema)
    body["text"]["format"]["name"] = name
    return body
