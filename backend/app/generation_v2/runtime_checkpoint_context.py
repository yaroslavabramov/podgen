from __future__ import annotations

import json
from typing import Any

from app.generation_v2.runtime_contracts import CheckpointResumeContext

MAX_CHECKPOINT_USER_INTENT_CHARS = 5_000
MAX_CHECKPOINT_WORKFLOW_CONTRACT_CHARS = 12_000
MAX_CHECKPOINT_TASK_CONTEXT_CHARS = 30_000


TRIAGE_PLANNER_RESUME_CONTRACT = """Continue only the current source-triage planning attempt.
Treat fresh durable workspace pages as authoritative, follow next_cursor until required sections are complete, and
never recreate accepted writes. Resume the unfinished planning work under the original prompt and finish only through
finalize_triage_workspace_plan."""


TRIAGE_SET_RESUME_CONTRACT = """Continue only the current source set from fresh durable state.
Restore set_overview and unfinished_work, then open source_memory for the current source, question_index for
question/requirement orientation, coverage_hints for saved hints, or finding_list only when needed.
Execute persisted retries for pending reads and replay_required_after_activation before advancing beyond those ranges. Never recreate completed source
analyses or hints. Do not answer coverage questions. Continue under the original prompt and finish only through
finish_triage_source_set."""


TRIAGE_FINALIZER_RESUME_CONTRACT = """Continue only the current source-triage finalization attempt.
Treat fresh persisted questions, findings, coverage hints, and map records as authoritative; page every section marked
incomplete and never recreate accepted writes. Do not answer coverage questions or create outcomes/syntheses.
Continue the unfinished source-mapping finalization under the original prompt and finish only through
finalize_source_map."""


RESEARCH_PLANNER_RESUME_CONTRACT = """Continue editorial-research planning only.
Treat the fresh durable CorpusBrief and persisted jobs as authoritative. Preserve accepted jobs and add or refine
only work required by a concrete unresolved handoff item. Use SourceMap pages only to refine scope. Do not read or
search sources, create findings or atoms, decide the memo thesis, or plan episode structure. Preserve missing and
deferred work honestly. Finish only through finalize_research_plan."""


RESEARCH_JOB_RESUME_CONTRACT = """Continue only the current editorial-research investigation job from fresh
durable state. Restore omitted job questions, observations, starting evidence, and SourceMap material through the
provided paging/read tools. Persist meaningful progress as observations, evidence-backed atoms, risks, or gaps.
Resolve every planned question with saved job evidence or explicitly defer it with a concrete reason. Do not redesign
the research plan, work on another job, finalize the memo, or write treatment. Finish only through
finish_research_job."""


RESEARCH_FINALIZER_RESUME_CONTRACT = """Continue editorial-research memo finalization from fresh persisted state.
Page the complete active atom index, inspect plausible overlaps, merge genuine duplicates, preserve risks and gaps,
and write only memo-level synthesis fields. Do not search broadly, create new findings, reopen the research plan,
decide episode acts, or write treatment. Finish only through finalize_research_memo."""


def build_checkpoint_resume_context(
    *,
    run_config: dict[str, Any],
    workflow_contract: str,
    show_bible: Any | None = None,
) -> CheckpointResumeContext:
    user_intent = str(run_config.get("user_prompt") or "")
    language = str(run_config.get("language") or "en").strip() or "en"
    duration_seconds = int(run_config.get("duration_target") or run_config.get("duration_seconds") or 1_800)
    task_context: dict[str, Any] = {
        "language": language,
        "duration_seconds": duration_seconds,
        "user_intent": user_intent,
    }
    if show_bible is not None:
        task_context["show_bible"] = _full_show_bible(show_bible)
    context = CheckpointResumeContext(
        workflow_contract=" ".join(str(workflow_contract or "").split()),
        task_context=task_context,
    )
    validate_checkpoint_resume_context(context)
    return context


def validate_checkpoint_resume_context(context: CheckpointResumeContext) -> None:
    contract = str(context.workflow_contract or "").strip()
    if not contract:
        raise RuntimeError("checkpoint resume workflow contract is required")
    if len(contract) > MAX_CHECKPOINT_WORKFLOW_CONTRACT_CHARS:
        raise RuntimeError("checkpoint resume workflow contract exceeds the model-facing contract")
    if context.contract_version != "v2.checkpoint_resume.v1":
        raise RuntimeError("unsupported checkpoint resume contract version")
    task_context = context.task_context
    if not isinstance(task_context, dict):
        raise RuntimeError("checkpoint resume task context must be an object")
    allowed_keys = {
        "language",
        "duration_seconds",
        "user_intent",
        "show_bible",
    }
    unexpected = sorted(set(task_context) - allowed_keys)
    if unexpected:
        raise RuntimeError(
            "checkpoint resume task context contains mutable or unsupported fields: " + ", ".join(unexpected)
        )
    language = str(task_context.get("language") or "").strip()
    if not language or len(language) > 40:
        raise RuntimeError("checkpoint resume language is missing or invalid")
    duration_seconds = int(task_context.get("duration_seconds") or 0)
    if duration_seconds <= 0:
        raise RuntimeError("checkpoint resume duration is missing or invalid")
    user_intent = str(task_context.get("user_intent") or "")
    if not user_intent.strip():
        raise RuntimeError("checkpoint resume user intent is required")
    if len(user_intent) > MAX_CHECKPOINT_USER_INTENT_CHARS:
        raise RuntimeError("checkpoint resume user intent exceeds the model-facing contract")
    if len(json.dumps(task_context, ensure_ascii=False)) > MAX_CHECKPOINT_TASK_CONTEXT_CHARS:
        raise RuntimeError("checkpoint resume task context exceeds the model-facing contract")


def _full_show_bible(show_bible: Any) -> dict[str, Any]:
    raw = show_bible.model_dump(mode="json") if hasattr(show_bible, "model_dump") else dict(show_bible or {})
    if not isinstance(raw, dict):
        raise RuntimeError("checkpoint resume ShowBible must be an object")
    # ShowBible is immutable task context and the resumed agent has no reader
    # for omitted style rules. Keep the validated value whole; the bounded task
    # context check above fails explicitly if it cannot fit.
    return raw


__all__ = [
    "CheckpointResumeContext",
    "RESEARCH_FINALIZER_RESUME_CONTRACT",
    "RESEARCH_JOB_RESUME_CONTRACT",
    "RESEARCH_PLANNER_RESUME_CONTRACT",
    "TRIAGE_FINALIZER_RESUME_CONTRACT",
    "TRIAGE_PLANNER_RESUME_CONTRACT",
    "TRIAGE_SET_RESUME_CONTRACT",
    "build_checkpoint_resume_context",
    "validate_checkpoint_resume_context",
]
