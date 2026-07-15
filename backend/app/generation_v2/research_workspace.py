from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.corpus_scope import scope_for_run_view, session_corpus_scope
from app.generation_v2.research_workspace_contracts import (
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_advisory import (
    RESEARCH_READ_TOOLS,
    RESEARCH_SEMANTIC_COMMIT_TOOLS,
    research_workflow_commit_advisory,
    research_workflow_guard_error,
)
from app.generation_v2.research_workspace_prompting import (
    research_finalizer_payload_for_prompt,
    research_job_prompt_payload,
    research_workspace_summary_for_prompt,
    source_map_planner_summary,
    source_map_research_summary,
)
from app.generation_v2.research_workspace_memo import research_memo_from_workspace
from app.generation_v2.research_workspace_persistence import (
    load_research_workspace,
    load_research_workspace_artifact,
    save_research_workspace,
)
from app.generation_v2.research_workspace_repair_queue import enqueue_research_repair_jobs
from app.generation_v2.research_workspace_repair_budget import (
    TARGETED_REPAIR_BUDGET,
    pending_repair_operation,
    repair_budget,
    repair_operations,
)
from app.generation_v2.research_workspace_gap_helpers import dedupe_gaps as _dedupe_gaps
from app.generation_v2.research_workspace_job_state import (
    invalid_job_evidence_ids as _invalid_job_evidence_ids,
    invalid_workspace_evidence_ids as _invalid_workspace_evidence_ids,
    invalid_workspace_refs as _invalid_workspace_refs,
    mark_research_progress as _mark_research_progress,
    prepare_workspace_jobs_for_resume as _prepare_workspace_jobs_for_resume,
    replace_workspace_job as _replace_workspace_job,
    refs_outside_job as _refs_outside_job,
    reset_research_read_advisory as _reset_research_read_advisory,
    resolve_workspace_atom_refs as _resolve_workspace_atom_refs,
    source_ids_for_refs as _source_ids_for_refs,
    source_order as _source_order,
    workspace_job as _workspace_job,
)
from app.generation_v2.research_workspace_state import (
    WORKSPACE_SCHEMA_VERSION,
    normalize_workspace as _normalize_workspace,
)
from app.generation_v2.research_workspace_helpers import (
    dedupe as _dedupe,
    now as _now,
    source_map_signature as _source_map_signature,
)
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import CorpusBrief, SourceMap
from app.generation_v2.source_triage_canonical import research_eligible
from app.generation_v2.corpus_brief_preview import corpus_brief_preview
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_run import GenerationRun


def _semantic_field_error(args: dict[str, Any], field_names: list[str]) -> dict[str, Any] | None:
    return semantic_field_error(args, SEMANTIC_FIELD_LIMITS, field_names)


def _research_source_order(source_map: SourceMap, source_inventory: SourceMap) -> list[str]:
    full_order = _source_order(source_inventory)
    if source_map.source_assessments:
        eligible_source_ids = {
            assessment.source_id for assessment in source_map.source_assessments if research_eligible(assessment)
        }
    elif source_map.source_triage.ranked_sources:
        eligible_source_ids = {item.source_id for item in source_map.source_triage.ranked_sources}
    else:
        return full_order
    context = build_source_ref_context(source_inventory)
    eligible_refs = {
        source_ref_for_id(card.source_id, context)
        for card in source_inventory.source_cards
        if card.source_id in eligible_source_ids
    }
    return [source_ref for source_ref in full_order if source_ref in eligible_refs]


async def ensure_research_workspace(
    db: AsyncSession,
    *,
    run: GenerationRun,
    source_map: SourceMap,
    source_inventory: SourceMap,
    run_view: Any | None = None,
    corpus_brief: CorpusBrief | None = None,
    corpus_brief_artifact_id: str | None = None,
) -> dict[str, Any]:
    existing = await load_research_workspace(db, run.id)
    scope = scope_for_run_view(run_view) if run_view is not None else session_corpus_scope(db, run.id)
    source_order = _research_source_order(source_map, source_inventory)
    source_map_signature = _source_map_signature(source_map, source_inventory)
    if (
        existing
        and existing.get("schema_version") == WORKSPACE_SCHEMA_VERSION
        and existing.get("source_order") == source_order
        and existing.get("source_map_signature") == source_map_signature
        and int(existing.get("corpus_revision") or 1) == scope.revision
        and int(existing.get("attempt") or 1) == scope.attempt
        and (existing.get("corpus_brief_artifact_id") or None) == corpus_brief_artifact_id
    ):
        return _normalize_workspace(existing)
    repair_resume = _repair_resume_state(existing or {}, source_order=source_order)
    seeded_jobs = (
        _jobs_from_corpus_brief(corpus_brief, source_inventory, allowed_source_refs=source_order)
        if corpus_brief is not None
        else []
    )
    handoff_question_ids = {
        question_id
        for item in (corpus_brief.research_handoff if corpus_brief is not None else [])
        for question_id in item.question_ids
    }
    brief_questions = (
        [item.question for item in corpus_brief.coverage_questions if item.question_id in handoff_question_ids]
        if corpus_brief is not None
        else []
    )
    resumed_jobs = [dict(item) for item in repair_resume.get("investigation_jobs") or [] if isinstance(item, dict)]
    investigation_jobs = _merge_corpus_handoff_jobs(resumed_jobs, seeded_jobs) if resumed_jobs else seeded_jobs
    research_questions = _dedupe_semantic_text(
        [
            *[str(item) for item in repair_resume.get("research_questions") or []],
            *brief_questions,
        ]
    )
    workspace = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "source_map_signature": source_map_signature,
        "corpus_revision": scope.revision,
        "attempt": scope.attempt,
        "status": repair_resume.get("status", "planning"),
        "source_order": source_order,
        "corpus_brief_artifact_id": corpus_brief_artifact_id,
        "corpus_brief": corpus_brief.model_dump(mode="json") if corpus_brief is not None else None,
        "corpus_brief_preview": (
            corpus_brief_preview(corpus_brief, source_inventory=source_inventory)
            if corpus_brief is not None
            else None
        ),
        "research_questions": research_questions,
        "investigation_jobs": investigation_jobs,
        "current_job": None,
        "processed_jobs": [],
        "covered_source_refs": [],
        "selected_evidence_ids": [],
        "atom_drafts": [],
        "open_questions": [],
        "research_gaps": repair_resume.get("research_gaps", []),
        "risk_register": [],
        "memo_fields": {},
        # Worker retries create a new corpus attempt. Repair limits are per
        # generation run. Pending operations also retain the exact work and
        # instructions needed to resume rather than spending a second pass.
        "repair_budget": repair_budget(existing or {}),
        "repair_operations": repair_resume.get("repair_operations", repair_operations(existing or {})),
        "audit_repair_feedback": repair_resume.get("audit_repair_feedback", []),
        "created_at": _now(),
        "updated_at": _now(),
    }
    await save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
    return workspace


def _jobs_from_corpus_brief(
    brief: CorpusBrief,
    source_inventory: SourceMap,
    *,
    allowed_source_refs: list[str] | None = None,
) -> list[dict[str, Any]]:
    ref_context = build_source_ref_context(source_inventory)
    source_order = set(allowed_source_refs if allowed_source_refs is not None else _source_order(source_inventory))
    question_by_id = {item.question_id: item for item in brief.coverage_questions}
    outcome_by_question_id = {item.question_id: item for item in brief.coverage_outcomes}
    kind_to_atom = {
        "context": "context",
        "origin": "context",
        "mechanism": "mechanism",
        "cause": "driver",
        "change": "change",
        "impact": "impact",
        "exception": "exception",
    }
    jobs: list[dict[str, Any]] = []
    for index, item in enumerate(brief.research_handoff, start=1):
        source_refs = [
            source_ref_for_id(source_id, ref_context)
            for source_id in item.preferred_source_ids
            if source_ref_for_id(source_id, ref_context) in source_order
        ]
        questions = [
            question_by_id[question_id].question for question_id in item.question_ids if question_id in question_by_id
        ]
        corpus_brief_outcomes = []
        for question_id in item.question_ids:
            outcome = outcome_by_question_id.get(question_id)
            if outcome is None:
                continue
            corpus_brief_outcomes.append(
                {
                    "question_id": outcome.question_id,
                    "status": str(outcome.status),
                    "next_action": str(outcome.next_action),
                    "source_refs": _dedupe(
                        [
                            source_ref_for_id(source_id, ref_context)
                            for source_id in outcome.source_ids
                            if source_ref_for_id(source_id, ref_context) in source_order
                        ]
                    ),
                    "evidence_ids": list(outcome.evidence_ids),
                    "orientation_summary": outcome.orientation_summary,
                    "missing_detail": outcome.missing_detail,
                    "conflict_summary": outcome.conflict_summary,
                    "deferred_reason": outcome.deferred_reason,
                    "confidence": outcome.confidence,
                }
            )
        required_atom_kinds = _dedupe(
            [
                kind_to_atom.get(str(question_by_id[question_id].kind), "")
                for question_id in item.question_ids
                if question_id in question_by_id
            ]
        )
        jobs.append(
            {
                "job_id": f"brief_{index:02d}",
                "corpus_handoff_id": item.handoff_id,
                "coverage_question_ids": list(item.question_ids),
                "corpus_brief_outcomes": corpus_brief_outcomes,
                "label": item.objective,
                "story_job": str(item.action),
                "purpose": item.reason or item.objective,
                "source_refs": _dedupe(source_refs),
                "questions": questions or [item.objective],
                "required_atom_kinds": required_atom_kinds,
                "priority": item.priority,
                "allow_broad_search": not bool(source_refs),
                "status": "planned",
                "observations": [],
                "atom_ids": [],
                # CorpusBrief evidence is an explicit reading queue, not a
                # completed research result. A job may promote one of these
                # ids into evidence_ids only after read_evidence succeeds.
                "starting_evidence_ids": list(item.starting_evidence_ids),
                "evidence_ids": [],
                "summary": "",
                "deferred_reason": "",
                "question_outcomes": [],
                "attempt": 0,
                "updated_at": _now(),
            }
        )
    return jobs


def _merge_corpus_handoff_jobs(
    resumed_jobs: list[dict[str, Any]],
    seeded_jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Carry repair work forward without losing the new corpus handoff."""

    remaining = [deepcopy(item) for item in resumed_jobs]
    merged: list[dict[str, Any]] = []
    for seed in seeded_jobs:
        seed_handoff_id = str(seed.get("corpus_handoff_id") or "")
        seed_question_ids = {str(item) for item in seed.get("coverage_question_ids") or [] if str(item).strip()}
        match_index = next(
            (
                index
                for index, job in enumerate(remaining)
                if (seed_handoff_id and str(job.get("corpus_handoff_id") or "") == seed_handoff_id)
                or bool(
                    seed_question_ids
                    & {str(item) for item in job.get("coverage_question_ids") or [] if str(item).strip()}
                )
            ),
            None,
        )
        if match_index is None:
            merged.append(deepcopy(seed))
            continue
        job = remaining.pop(match_index)
        job["corpus_handoff_id"] = seed_handoff_id
        job["coverage_question_ids"] = _dedupe(
            [
                *[str(item) for item in seed.get("coverage_question_ids") or []],
                *[str(item) for item in job.get("coverage_question_ids") or []],
            ]
        )
        job["source_refs"] = _dedupe(
            [
                *[str(item) for item in seed.get("source_refs") or []],
                *[str(item) for item in job.get("source_refs") or []],
            ]
        )
        job["questions"] = _dedupe_semantic_text(
            [
                *[str(item) for item in seed.get("questions") or []],
                *[str(item) for item in job.get("questions") or []],
            ]
        )
        job["starting_evidence_ids"] = _dedupe(
            [
                *[str(item) for item in seed.get("starting_evidence_ids") or []],
                *[str(item) for item in job.get("starting_evidence_ids") or []],
            ]
        )
        job["corpus_brief_outcomes"] = deepcopy(seed.get("corpus_brief_outcomes") or [])
        merged.append(job)
    return [*merged, *remaining]


def _dedupe_semantic_text(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        key = " ".join(text.split()).casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _repair_resume_state(existing: dict[str, Any], *, source_order: list[str]) -> dict[str, Any]:
    operation = pending_repair_operation(existing, TARGETED_REPAIR_BUDGET)
    if operation is None:
        return {}
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    raw_jobs = payload.get("resume_jobs") or existing.get("investigation_jobs") or []
    jobs = [_job_plan_for_new_attempt(item, source_order=source_order) for item in raw_jobs if isinstance(item, dict)]
    gaps = [
        _gap_for_new_attempt(item, source_order=source_order)
        for item in (payload.get("research_gaps") or existing.get("research_gaps") or [])
        if isinstance(item, dict)
    ]
    feedback = deepcopy(payload.get("audit_repair_feedback") or existing.get("audit_repair_feedback") or [])
    questions = deepcopy(payload.get("research_questions") or existing.get("research_questions") or [])
    operations = repair_operations(existing)
    carried_operation = operations.get(TARGETED_REPAIR_BUDGET)
    if carried_operation is not None:
        carried_payload = dict(carried_operation.get("payload") or {})
        carried_payload.update(
            {
                "resume_jobs": deepcopy(jobs),
                "research_questions": deepcopy(questions),
                "research_gaps": deepcopy(gaps),
                "audit_repair_feedback": deepcopy(feedback),
            }
        )
        carried_operation["payload"] = carried_payload
        operations[TARGETED_REPAIR_BUDGET] = carried_operation
    return {
        "status": "repair_planned" if jobs else "planning",
        "research_questions": questions,
        "investigation_jobs": jobs,
        "research_gaps": gaps,
        "audit_repair_feedback": feedback,
        "repair_operations": operations,
    }


def _job_plan_for_new_attempt(job: dict[str, Any], *, source_order: list[str]) -> dict[str, Any]:
    current = deepcopy(job)
    valid_refs = set(source_order)
    current["source_refs"] = [str(ref) for ref in current.get("source_refs") or [] if str(ref) in valid_refs]
    if not current["source_refs"]:
        current["allow_broad_search"] = True
    current.update(
        {
            "status": "planned",
            "observations": [],
            "atom_ids": [],
            "starting_evidence_ids": [],
            "evidence_ids": [],
            "summary": "",
            "deferred_reason": "",
            "question_outcomes": [],
            # CorpusBrief lineage belongs to one immutable corpus attempt.
            # A matching fresh handoff repopulates it during merge below.
            "corpus_brief_outcomes": [],
            "attempt": 0,
            "resume_reason": "Repair operation resumed in a new corpus attempt.",
            "updated_at": _now(),
        }
    )
    current.pop("started_at", None)
    repair_gap = current.get("repair_gap")
    if isinstance(repair_gap, dict):
        repair_gap = deepcopy(repair_gap)
        repair_gap["source_refs"] = [str(ref) for ref in repair_gap.get("source_refs") or [] if str(ref) in valid_refs]
        repair_gap["evidence_ids"] = []
        current["repair_gap"] = repair_gap
    return current


def _gap_for_new_attempt(gap: dict[str, Any], *, source_order: list[str]) -> dict[str, Any]:
    current = deepcopy(gap)
    valid_refs = set(source_order)
    current["source_refs"] = [str(ref) for ref in current.get("source_refs") or [] if str(ref) in valid_refs]
    current["evidence_ids"] = []
    if current.get("status") == "resolved":
        current["status"] = "open"
        current["resolution_note"] = "Resolution must be re-established in the current corpus attempt."
    return current


def build_research_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    source_map: SourceMap | None = None,
    evidence_toolset: Any | None = None,
    mode: str,
    current_job_id: str | None = None,
) -> list[AgentTool]:
    from app.generation_v2.research_workspace_tools import (
        build_research_workspace_tools as _build_research_workspace_tools,
    )

    return _build_research_workspace_tools(
        db=db,
        run=run,
        workspace=workspace,
        source_inventory=source_inventory,
        source_map=source_map,
        evidence_toolset=evidence_toolset,
        mode=mode,
        current_job_id=current_job_id,
    )


async def start_research_job(
    db: AsyncSession,
    *,
    run: GenerationRun,
    workspace: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    current = await load_research_workspace(db, run.id) or workspace
    job = _workspace_job(current, job_id)
    if job is None:
        raise RuntimeError(f"Unknown research job: {job_id}")
    job["status"] = "in_progress"
    job["attempt"] = int(job.get("attempt") or 0) + 1
    job["started_at"] = _now()
    job["updated_at"] = _now()
    _replace_workspace_job(current, job)
    current["current_job"] = {
        "job_id": job_id,
        "started_at": job["started_at"],
        "source_refs": job.get("source_refs") or [],
    }
    _reset_research_read_advisory(current)
    await save_research_workspace(db, generation_run_id=run.id, workspace=current)
    return current


def unprocessed_research_jobs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("investigation_jobs") or []
        if item.get("status") == "planned"
    ]


async def prepare_research_jobs_for_resume(
    db: AsyncSession,
    *,
    run: GenerationRun,
    workspace: dict[str, Any],
    save_workspace: Any = save_research_workspace,
) -> tuple[dict[str, Any], list[str]]:
    current, resumed_job_ids = _prepare_workspace_jobs_for_resume(workspace)
    if resumed_job_ids:
        await save_workspace(db, generation_run_id=run.id, workspace=current)
    return current, resumed_job_ids


def unresolved_research_jobs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("investigation_jobs") or []
        if item.get("status") in {"planned", "in_progress"}
    ]


__all__ = [
    "RESEARCH_READ_TOOLS",
    "RESEARCH_SEMANTIC_COMMIT_TOOLS",
    "build_research_workspace_tools",
    "enqueue_research_repair_jobs",
    "ensure_research_workspace",
    "_dedupe_gaps",
    "_invalid_job_evidence_ids",
    "_invalid_workspace_evidence_ids",
    "_invalid_workspace_refs",
    "load_research_workspace",
    "load_research_workspace_artifact",
    "_mark_research_progress",
    "_refs_outside_job",
    "_replace_workspace_job",
    "_reset_research_read_advisory",
    "_resolve_workspace_atom_refs",
    "research_finalizer_payload_for_prompt",
    "research_job_prompt_payload",
    "research_memo_from_workspace",
    "research_workspace_summary_for_prompt",
    "research_workflow_commit_advisory",
    "research_workflow_guard_error",
    "save_research_workspace",
    "prepare_research_jobs_for_resume",
    "_semantic_field_error",
    "_source_ids_for_refs",
    "source_map_planner_summary",
    "source_map_research_summary",
    "start_research_job",
    "unprocessed_research_jobs",
    "unresolved_research_jobs",
]
