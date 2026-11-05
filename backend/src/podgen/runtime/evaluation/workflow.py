"""Explicit evaluation stages/profiles; fixed-plan keeps its historical three-stage contract."""

from podgen.writing.roles import EditorialFeedback, WriteOutput

from .contracts import ALL_STAGES, STAGES


def stages_for(command):
    return ALL_STAGES if command.plan_mode == "own-plan" else STAGES


def profile_for(command, stage):
    if stage in ("plan", "plan-revise"):
        return command.planner_profile_ref
    if stage in ("plan-review", "plan-review-revised"):
        return command.plan_reviewer_profile_ref
    return command.reviewer_profile_ref if stage == "review" else command.writer_profile_ref


def output_model(stage):
    if stage.startswith("plan"):
        from podgen.writing.planning_roles import PlanningOutput, PlanReviewOutput

        return PlanReviewOutput if stage.startswith("plan-review") else PlanningOutput
    return EditorialFeedback if stage == "review" else WriteOutput


def prompt_stage(stage):
    return "plan-review" if stage == "plan-review-revised" else stage
