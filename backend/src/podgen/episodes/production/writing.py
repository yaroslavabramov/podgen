"""Native writing commands and cumulative allowances across material continuations."""

from uuid import UUID, uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.contracts import RunLimits, WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal


def command(config, journal, progress):
    runs = progress.get("writing_runs", [])
    parent = UUID(runs[-1]) if runs else None
    run_id = uuid5(config.run_id, "writing/1:" + str(len(runs)))
    used = dict(input=0, output=0)
    for result in progress.get("writing_results", {}).values():
        for field, value in result["token_usage_or_reservation"].items():
            if field in used:
                used[field] += value
    original = config.settings.writing_limits
    from podgen.runtime.evaluation import cost_budget

    # Under run-cost-budget/1 the USD limit is shared through the material chain at manifest
    # creation; token ceilings are not enforced and are passed unchanged.
    budgeted = parent is not None and cost_budget.active(journal.manifest(parent)) is not None
    if not budgeted and (
        used["input"] >= original.max_total_input_tokens or used["output"] >= original.max_total_output_tokens
    ):
        raise ContractError("episode_writing_budget_exhausted")
    fixed_plan = None
    if parent is not None:
        previous = saved_command(journal.repo, journal.project, parent)
        if previous.fixed_plan_input_ref is not None:
            fixed_plan = previous.fixed_plan_input_ref
        else:
            from podgen.runtime.evaluation.reusable_plan import export_plan_ref

            fixed_plan = export_plan_ref(journal, parent)
    return WritingRunCommand(
        run_id=run_id,
        project_id=journal.project,
        actor_id=journal.repo.actor_id,
        case_id=str(config.run_id),
        author_ref=RuntimeRef.model_validate(progress["materials_ref"]),
        writer_profile_ref=config.settings.writer_profile_ref,
        reviewer_profile_ref=config.settings.reviewer_profile_ref,
        planner_profile_ref=config.settings.planner_profile_ref if parent is None else None,
        plan_reviewer_profile_ref=config.settings.plan_reviewer_profile_ref if parent is None else None,
        plan_mode="own-plan" if parent is None else "fixed-plan",
        fixed_plan_input_ref=fixed_plan,
        review_mode="independent-readings/1",
        max_plan_revisions=1,
        max_script_revisions=2 - progress.get("revisions_used", 0),
        initial_script_ref=progress.get("continuation_script_ref"),
        material_parent_run_id=parent,
        limits=original
        if budgeted
        else RunLimits(
            max_total_input_tokens=original.max_total_input_tokens - used["input"],
            max_total_output_tokens=original.max_total_output_tokens - used["output"],
            max_elapsed_seconds=original.max_elapsed_seconds,
        ),
    )


def saved_command(repo, project, run_id):
    return WritingRunCommand.model_validate(EvaluationJournal(repo, project).manifest(run_id)["command"])


def completed_revisions(journal, run_id):
    count = 0
    for stage in ("revise-1", "revise-2"):
        row = journal.invocation(run_id, stage)
        if row and row["status"] == "completed":
            value = journal.output(row)
            if value.get("contract") != "writer-material-request/1":
                count += 1
    return count


def material_questions(journal, result):
    if result.get("material_request"):
        return [result["material_request"]], result.get("last_script_ref")
    if result.get("review_status") != "needs_material":
        return [], None
    outcome = journal.read(RuntimeRef.model_validate(result["outcome_ref"]))
    from .material_requests import collect

    return collect(journal, outcome)


def pending_handoff(journal, run_id, invocation_id):
    from podgen.runtime.evaluation.workflow import stages_for

    cmd = saved_command(journal.repo, journal.project, run_id)
    invocation_id = UUID(str(invocation_id))
    # Only an actual pending tool result is actionable. Do not turn arbitrary
    # needs_attention failures into a new paid research commission.
    for stage in stages_for(cmd):
        root = journal.invocation(run_id, stage)
        if root is None or root["status"] == "completed" or root["invocation_id"] != invocation_id:
            continue
        resolved = set()
        for event in reversed(journal.events(root)):
            if event["event_kind"] not in ("source_material_pending", "source_tool_result"):
                continue
            record = journal.read(RuntimeRef(record_kind="tool_execution", record_id=event["record_id"]))
            data = record["data"]
            key = (data.get("call_index"), data.get("call_id"), data.get("arguments_hash"))
            if event["event_kind"] == "source_tool_result":
                resolved.add(key)
                continue
            if key in resolved:
                continue
            result = record["data"]["result"]
            if result.get("contract") in ("targeted-research-handoff/1", "targeted-research-handoff/2"):
                return result
    raise ContractError("episode_research_handoff_missing")
