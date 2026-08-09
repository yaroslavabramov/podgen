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
        if item.findings or item.gaps or item.source_refs or item.evidence_ids:
            questions = [
                question_by_id[question_id].question
                for question_id in item.question_ids
                if question_id in question_by_id
            ]
            source_refs = [ref for ref in item.source_refs if ref in source_order]
            coverage_requirements = [
                {
                    "question_id": item.question_ids[0] if item.question_ids else "",
                    "requirement_id": requirement.requirement_id,
                    "description": requirement.description,
                    "rationale": requirement.rationale,
                    "completion_criterion": requirement.completion_criterion,
                    "result": {"status": "unknown", "finding_ids": []},
                }
                for requirement in item.requirements
            ]
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
                    "corpus_handoff_question_assignments": [
                        assignment.model_dump(mode="json")
                        for assignment in item.question_assignments
                    ],
                    "corpus_handoff_finding_ids": [
                        str(finding.get("finding_id") or "")
                        for finding in item.findings
                        if isinstance(finding, dict) and str(finding.get("finding_id") or "")
                    ],
                    "corpus_handoff_source_refs": list(source_refs),
                    "corpus_handoff_evidence_ids": list(item.evidence_ids),
                    "corpus_handoff_gaps": deepcopy(item.gaps),
                    "corpus_brief_outcomes": [],
                    "coverage_requirements": coverage_requirements,
                    "open_requirements": coverage_requirements,
                    "source_findings": deepcopy(item.findings),
                    "source_gaps": deepcopy(item.gaps),
                    "source_cards": deepcopy(item.source_cards),
                    "label": item.objective,
                    "story_job": "deep_read",
                    "purpose": item.reason or item.objective,
                    "source_refs": _dedupe(source_refs),
                    "questions": questions or [item.objective],
                    "required_atom_kinds": required_atom_kinds,
                    "priority": item.priority,
                    "allow_broad_search": True,
                    "status": "planned",
                    "observations": [],
                    "atom_ids": [],
                    "starting_evidence_ids": _dedupe(
                        [
                            *item.evidence_ids,
                            *[
                                str(evidence_id)
                                for finding in item.findings
                                if isinstance(finding, dict)
                                for evidence_id in finding.get("evidence_ids") or []
                            ],
                        ]
                    ),
                    "evidence_ids": [],
                    "summary": "",
                    "deferred_reason": "",
                    "question_outcomes": [],
                    "attempt": 0,
                    "updated_at": _now(),
                }
            )
            continue
        handoff_finding_ids = set(item.finding_ids)
        handoff_evidence_ids = set(item.starting_evidence_ids)
        allowed_source_ids = list(item.allowed_source_ids or item.preferred_source_ids)
        source_refs = [
            source_ref_for_id(source_id, ref_context)
            for source_id in allowed_source_ids
            if source_ref_for_id(source_id, ref_context) in source_order
        ]
        preferred_source_refs = [
            source_ref_for_id(source_id, ref_context)
            for source_id in item.preferred_source_ids
            if source_ref_for_id(source_id, ref_context) in source_order
        ]
        candidate_source_refs = [
            source_ref_for_id(source_id, ref_context)
            for source_id in item.candidate_source_ids
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
                    "source_refs": list(source_refs),
                    "evidence_ids": [
                        evidence_id
                        for evidence_id in outcome.evidence_ids
                        if evidence_id in handoff_evidence_ids
                    ],
                    "finding_ids": [
                        finding_id
                        for finding_id in outcome.finding_ids
                        if finding_id in handoff_finding_ids
                    ],
                    "requirement_results": [
                        {
                            **result.model_dump(mode="json"),
                            "finding_ids": [
                                finding_id
                                for finding_id in result.finding_ids
                                if finding_id in handoff_finding_ids
                            ],
                        }
                        for result in outcome.requirement_results
                    ],
                    "orientation_summary": outcome.orientation_summary,
                    "missing_detail": outcome.missing_detail,
                    "conflict_summary": outcome.conflict_summary,
                    "deferred_reason": outcome.deferred_reason,
                    "confidence": outcome.confidence,
                    "semantic_revision": outcome.semantic_revision,
                    "is_stale": outcome.is_stale,
                }
            )
        coverage_requirements = _job_coverage_requirements(
            item.question_ids,
            question_by_id=question_by_id,
            outcome_by_question_id=outcome_by_question_id,
            allowed_finding_ids=handoff_finding_ids,
        )
        handoff_requirement_ids = {
            requirement_id
            for assignment in item.question_assignments
            for requirement_id in assignment.requirement_ids
        }
        has_handoff_requirement_scope = bool(item.question_assignments)
        source_findings = _job_source_findings(
            brief,
            question_ids=set(item.question_ids),
            finding_ids=set(item.finding_ids),
            candidate_finding_ids=set(item.candidate_finding_ids),
            source_order=source_order,
            allowed_source_refs=set(source_refs),
            ref_context=ref_context,
        )
        scoped_finding_ids = {
            str(finding.get("finding_id") or "")
            for finding in source_findings
            if str(finding.get("finding_id") or "")
        }
        inaccessible_finding_ids = sorted(set(item.finding_ids) - scoped_finding_ids)
        if inaccessible_finding_ids:
            raise ValueError(
                "CorpusBrief handoff findings are outside the job's preferred source scope: "
                + ", ".join(inaccessible_finding_ids)
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
                "corpus_handoff_question_assignments": [
                    assignment.model_dump(mode="json")
                    for assignment in item.question_assignments
                ],
                "corpus_handoff_finding_ids": list(item.finding_ids),
                "corpus_handoff_final_finding_ids": list(item.final_finding_ids),
                "corpus_handoff_candidate_finding_ids": list(item.candidate_finding_ids),
                "corpus_handoff_allowed_source_refs": _dedupe(source_refs),
                "corpus_handoff_preferred_source_refs": _dedupe(preferred_source_refs),
                "corpus_handoff_candidate_source_refs": _dedupe(candidate_source_refs),
                "corpus_handoff_candidate_evidence_ids": list(item.candidate_evidence_ids),
                "corpus_handoff_coverage_hint_ids": list(item.coverage_hint_ids),
                "corpus_brief_outcomes": corpus_brief_outcomes,
                "coverage_requirements": coverage_requirements,
                "open_requirements": [
                    requirement
                    for requirement in coverage_requirements
                    if str((requirement.get("result") or {}).get("next_action") or "research")
                    == "research"
                    and (
                        not has_handoff_requirement_scope
                        or str(requirement.get("requirement_id") or "") in handoff_requirement_ids
                    )
                ],
                "source_findings": source_findings,
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
                "starting_evidence_ids": _dedupe(
                    [
                        *item.starting_evidence_ids,
                        *[
                            str(evidence_id)
                            for finding in source_findings
                            for evidence_id in finding.get("evidence_ids") or []
                        ],
                    ]
                ),
                "evidence_ids": [],
                "summary": "",
                "deferred_reason": "",
                "question_outcomes": [],
                "attempt": 0,
                "updated_at": _now(),
            }
        )
    return jobs


def _job_coverage_requirements(
    question_ids: list[str],
    *,
    question_by_id: dict[str, Any],
    outcome_by_question_id: dict[str, Any],
    allowed_finding_ids: set[str],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for question_id in question_ids:
        question = question_by_id.get(question_id)
        if question is None:
            continue
        outcome = outcome_by_question_id.get(question_id)
        result_by_id = {
            item.requirement_id: item
            for item in (outcome.requirement_results if outcome is not None else [])
        }
        for requirement in question.answer_requirements:
            result = result_by_id.get(requirement.requirement_id)
            values.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement.requirement_id,
                    "description": requirement.description,
                    "rationale": requirement.rationale,
                    "completion_criterion": requirement.completion_criterion,
                    "result": (
                        {
                            **result.model_dump(mode="json"),
                            "finding_ids": [
                                finding_id
                                for finding_id in result.finding_ids
                                if finding_id in allowed_finding_ids
                            ],
                        }
                        if result is not None
                        else {"status": "unknown", "finding_ids": []}
                    ),
                }
            )
    return values


def _job_source_findings(
    brief: CorpusBrief,
    *,
    question_ids: set[str],
    finding_ids: set[str],
    candidate_finding_ids: set[str],
    source_order: set[str],
    allowed_source_refs: set[str],
    ref_context: Any,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for finding in getattr(brief, "source_findings", []) or []:
        if finding.finding_id not in finding_ids:
            continue
        links = [link for link in finding.question_links if link.question_id in question_ids]
        possible_question_ids = [
            question_id
            for question_id in finding.possible_question_ids
            if question_id in question_ids
        ]
        is_candidate_handoff = finding.finding_id in candidate_finding_ids
        if not links and not possible_question_ids and not is_candidate_handoff:
            continue
        source_ref = source_ref_for_id(finding.source_id, ref_context)
        if source_ref not in source_order or source_ref not in allowed_source_refs:
            continue
        source_relations: list[dict[str, Any]] = []
        for relation in finding.source_relations:
            related_source_ref = source_ref_for_id(relation.related_source_id, ref_context)
            if related_source_ref not in source_order or related_source_ref not in allowed_source_refs:
                continue
            source_relations.append(
                {
                    "related_source_ref": related_source_ref,
                    "relationship": str(relation.relationship),
                    "rationale": relation.rationale,
                    "evidence_ids": list(relation.evidence_ids),
                }
            )
        values.append(
            {
                "finding_id": finding.finding_id,
                "source_ref": source_ref,
                "statement": finding.statement,
                "evidence_ids": list(finding.evidence_ids),
                "scope": finding.scope.model_dump(mode="json"),
                "limitations": list(finding.limitations),
                "possible_question_ids": possible_question_ids,
                "handoff_link_kind": "candidate" if is_candidate_handoff and not links else "final",
                "question_links": [link.model_dump(mode="json") for link in links],
                "source_relations": source_relations,
                "semantic_revision": finding.semantic_revision,
            }
        )
    return values


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
        for field in (
            "corpus_handoff_source_refs",
            "corpus_handoff_evidence_ids",
            "corpus_handoff_gaps",
            "source_gaps",
            "source_cards",
        ):
            if seed.get(field):
                job[field] = deepcopy(seed.get(field))
        job["corpus_handoff_question_assignments"] = deepcopy(
            seed.get("corpus_handoff_question_assignments") or []
        )
        job["corpus_handoff_finding_ids"] = list(seed.get("corpus_handoff_finding_ids") or [])
        job["corpus_handoff_final_finding_ids"] = list(
            seed.get("corpus_handoff_final_finding_ids") or []
        )
        job["corpus_handoff_candidate_finding_ids"] = list(
            seed.get("corpus_handoff_candidate_finding_ids") or []
        )
        job["corpus_handoff_allowed_source_refs"] = list(
            seed.get("corpus_handoff_allowed_source_refs") or []
        )
        job["corpus_handoff_preferred_source_refs"] = list(
            seed.get("corpus_handoff_preferred_source_refs") or []
        )
        job["corpus_handoff_candidate_source_refs"] = list(
            seed.get("corpus_handoff_candidate_source_refs") or []
        )
        job["corpus_handoff_candidate_evidence_ids"] = list(
            seed.get("corpus_handoff_candidate_evidence_ids") or []
        )
        job["corpus_handoff_coverage_hint_ids"] = list(
            seed.get("corpus_handoff_coverage_hint_ids") or []
        )
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
        job["coverage_requirements"] = deepcopy(seed.get("coverage_requirements") or [])
        job["open_requirements"] = deepcopy(seed.get("open_requirements") or [])
        job["source_findings"] = deepcopy(seed.get("source_findings") or [])
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
            "corpus_handoff_question_assignments": [],
            "corpus_handoff_finding_ids": [],
            "corpus_handoff_final_finding_ids": [],
            "corpus_handoff_candidate_finding_ids": [],
            "corpus_handoff_allowed_source_refs": [],
            "corpus_handoff_preferred_source_refs": [],
            "corpus_handoff_candidate_source_refs": [],
            "corpus_handoff_candidate_evidence_ids": [],
            "corpus_handoff_coverage_hint_ids": [],
            "corpus_handoff_source_refs": [],
            "corpus_handoff_evidence_ids": [],
            "corpus_handoff_gaps": [],
            "coverage_requirements": [],
            "open_requirements": [],
            "source_findings": [],
            "source_gaps": [],
            "source_cards": [],
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
