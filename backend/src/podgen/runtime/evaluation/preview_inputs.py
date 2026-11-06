"""Admit only stage inputs that the ordinary runner can supply."""

from podgen.domain.errors import ContractError
from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput
from podgen.writing.roles import EditorialFeedback, WriteOutput

from .contracts import RunFailure
from .finalization import review_action
from .reading_workflow import ALL_READING_STAGES, BASES, base_stage

REQUIRED = {
    "plan": set(),
    "plan-review": {"candidate"},
    "plan-review-revised": {"candidate"},
    "plan-revise": {"candidate", "plan_review"},
    "reader-review": {"draft"},
    "fact-review": {"draft"},
    "revise-1": {"draft", "reader_feedback", "fact_feedback"},
    "revise-2": {"draft", "reader_feedback", "fact_feedback"},
    "draft": set(),
    "review": {"draft"},
    "revise": {"draft", "feedback"},
    "anchor-repair": {"draft", "feedback"},
    "plan-target-repair": {"candidate", "plan_review"},
}
READINGS = {stage for stage in ALL_READING_STAGES if base_stage(stage) in BASES}
REQUIRED.update({stage: {"draft"} for stage in READINGS})
WRITING = {"draft", "review", "revise"}


def validate_preview_inputs(stage, *, fixed, inputs):
    if stage not in REQUIRED:
        raise ContractError("unknown_preview_stage")
    supplied = {key for key, value in inputs.items() if value is not None}
    required = REQUIRED[stage]
    allowed = required | ({"approved_plan"} if stage in WRITING else set())
    if stage in ("revise-1", "revise-2"):
        allowed |= {"approved_plan"}
    if supplied - allowed or fixed and stage not in (WRITING | READINGS | {"revise-1", "revise-2"}):
        raise ContractError("preview_incompatible_inputs")
    if required - supplied:
        raise ContractError("preview_required_input_missing", ",".join(sorted(required - supplied)))
    if stage in WRITING:
        if fixed and "approved_plan" in supplied:
            raise ContractError("preview_plan_source_conflict")
        if not fixed and "approved_plan" not in supplied:
            raise ContractError("preview_plan_source_required")


def validate_preview_snapshots(stage, materials, inputs):
    for key in ("candidate", "approved_plan"):
        value = inputs.get(key)
        if value is not None:
            plan = PlanningOutput.model_validate(value)
            plan.validate_against(
                material_locators=materials.material_catalog, speaker_ids=materials.speaker_labels
            )
            if plan.status != "ready":
                raise ContractError("preview_plan_not_ready")
    if inputs.get("draft") is not None:
        script = WriteOutput.model_validate(inputs["draft"])
        if {row.speaker for row in script.utterances} != set(materials.speaker_labels):
            raise ContractError("preview_speaker_assignment_mismatch")
    if inputs.get("plan_review") is not None:
        review = PlanReviewOutput.model_validate(inputs["plan_review"])
        candidate = PlanningOutput.model_validate(inputs["candidate"])
        if stage == "plan-target-repair":
            review.validate_context(candidate, material_locators=materials.material_catalog)
        else:
            review.validate_against(candidate, material_locators=materials.material_catalog)
            if review.decision != "revise":
                raise ContractError("preview_plan_revision_not_required")
    if inputs.get("feedback") is not None:
        review = EditorialFeedback.model_validate(inputs["feedback"])
        if review.reading_mode != "informed":
            raise ContractError("preview_review_access_mode_mismatch")
        if stage == "revise":
            review.validate_against(WriteOutput.model_validate(inputs["draft"]))
            try:
                action = review_action(inputs["feedback"])
            except RunFailure as error:
                raise ContractError(error.code) from None
            if action != "revise":
                raise ContractError("preview_revision_not_required")

    for field, mode in (("reader_feedback", "script_only"), ("fact_feedback", "informed")):
        if inputs.get(field) is not None:
            from podgen.writing.fact_review import FactFeedback

            model = FactFeedback if field == "fact_feedback" else EditorialFeedback
            review = model.model_validate(inputs[field])
            review.validate_against(WriteOutput.model_validate(inputs["draft"]))
            if review.reading_mode != mode:
                raise ContractError("preview_review_access_mode_mismatch")
            if field == "fact_feedback" and any(
                set(f.support_labels) - set(materials.material_catalog) for f in review.findings
            ):
                raise ContractError("fact_finding_unknown_support_label")
