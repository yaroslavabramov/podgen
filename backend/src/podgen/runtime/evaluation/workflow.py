"""Versioned role stages and profiles; historical runs retain their three-stage contract."""

from podgen.writing.projection_versions import V1
from podgen.writing.roles import EditorialFeedback, WriteOutput

from .contracts import ALL_STAGES, PLANNING_STAGES, STAGES
from .reading_workflow import base_stage, independent, reading_stages


def stages_for(command):
    if command.editorial_parent_run_id is not None:
        from .editorial_continuation.source import STAGES as continuation_stages

        return continuation_stages
    if command.planning_parent_run_id is not None:
        writing = reading_stages(command.max_script_revisions) if independent(command) else STAGES
        return ("plan-revise", "plan-review-revised", *writing)
    if independent(command):
        planning = PLANNING_STAGES if command.plan_mode == "own-plan" else ()
        return (*planning, *reading_stages(command.max_script_revisions))
    return ALL_STAGES if command.plan_mode == "own-plan" else STAGES


def profile_for(command, stage):
    stage = base_stage(stage)
    if stage in ("plan", "plan-revise"):
        return command.planner_profile_ref
    if stage in ("plan-review", "plan-review-revised"):
        return command.plan_reviewer_profile_ref
    return (
        command.reviewer_profile_ref
        if stage in ("review", "reader-review", "fact-review")
        else command.writer_profile_ref
    )


def output_model(
    stage,
    *,
    reader_schema_version=None,
    fact_schema_version=None,
    projection_version=V1,
    planning_output_contract=None,
    revision_verification_contract=None,
):
    from podgen.writing.reading_audit import FACT_SCHEMA, READER_SCHEMA, RECHECK_SCHEMA
    from podgen.writing.reading_audit.registry import model as audit_model

    base = base_stage(stage)
    if base in ("reader-review", "fact-review") and (
        reader_schema_version == READER_SCHEMA
        or fact_schema_version == FACT_SCHEMA
        or revision_verification_contract == RECHECK_SCHEMA
    ):
        return audit_model(stage)
    from podgen.writing.revision.verification import applies, feedback_model

    if applies(stage, revision_verification_contract):
        return feedback_model(revision_verification_contract)
    stage = base_stage(stage)
    if stage == "fact-review":
        from podgen.writing.fact_review import feedback_model

        return feedback_model(fact_schema_version)
    if stage == "reader-review":
        from podgen.writing.reader_schema import feedback_model

        return feedback_model(reader_schema_version)
    if stage.startswith("plan"):
        from podgen.writing.planning_roles import PlanReviewOutput
        from podgen.writing.planning_versions import planning_model

        return (
            PlanReviewOutput
            if stage.startswith("plan-review")
            else planning_model(projection_version, planning_output_contract=planning_output_contract)
        )
    return EditorialFeedback if stage == "review" else WriteOutput


def prompt_stage(stage):
    stage = base_stage(stage)
    return "plan-review" if stage == "plan-review-revised" else stage
