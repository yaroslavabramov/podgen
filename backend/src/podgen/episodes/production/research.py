"""Reuse prepared hosted jobs and the existing run-scoped material resolver."""

import json
from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.research.hosted.contracts import Approval
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.service import advance, approve


def advance_job(parent, config, progress, run_id, provider):
    identity = str(run_id)
    jobs = progress.setdefault("research_jobs", [])
    if identity not in jobs:
        if len(jobs) >= config.settings.max_research_jobs:
            raise ContractError("episode_research_job_allowance_exhausted")
        jobs.append(identity)
    child = ResearchJournal(parent.repo, parent.project, UUID(identity))
    manifest = child.get("manifest")
    if manifest is None:
        raise ContractError("episode_research_job_not_prepared")
    if child.get("approval") is None:
        approve(
            child,
            Approval(
                request_hash=manifest["request_hash"],
                reason=f"Episode {config.run_id}: bounded research allowance in its immutable configuration",
                accepts_variable_provider_cost=config.settings.accepts_variable_research_cost,
            ),
        )
    state = advance(child, provider)
    if state["state"] == "completed":
        return state
    if state["state"] not in ("in_progress", "prepared", "result_processing_required"):
        raise ContractError("episode_research_" + state["state"])
    if state.get("operation_error"):
        raise ContractError("episode_research_" + state["operation_error"])
    return state


def terminal_question(questions):
    if not questions:
        raise ContractError("episode_material_request_has_no_concrete_question")
    if len(questions) == 1:
        return questions[0]
    return dict(
        question="Research the complete set of outstanding review questions together:\n"
        + json.dumps(questions, ensure_ascii=False, indent=2, sort_keys=True),
        reason="Resolve the complete known material gap before the script continues.",
    )


def resolve_terminal(parent, writing_runner, progress, question):
    from .writing import saved_command

    run_id = UUID(progress["writing_run_id"])
    command = saved_command(parent.repo, parent.project, run_id)
    materials = writing_runner._materials(command)
    resolver = writing_runner.material_resolver(command, materials)
    operation = uuid5(parent.run_id, "terminal-material/1:" + digest(question))
    return resolver.resolve(
        "request_research",
        dict(
            question=question["question"],
            reason=question.get("why_needed") or question.get("reason") or question["question"],
        ),
        operation_id=operation,
        run_id=run_id,
        guard=lambda: None,
    ).result
