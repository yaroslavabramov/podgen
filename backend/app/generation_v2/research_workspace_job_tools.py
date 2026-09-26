from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import research_workspace as _workspace
from app.generation_v2.persistence_contracts import semantic_list_error
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_PENDING_QUESTION_LIMIT,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_helpers import dedupe as _dedupe
from app.generation_v2.research_workspace_helpers import gap_key as _gap_key
from app.generation_v2.research_workspace_helpers import now as _now
from app.generation_v2.research_workspace_helpers import stable_id as _stable_id
from app.generation_v2.research_workspace_helpers import active_atom_drafts as _active_atom_drafts
from app.models.generation_run import GenerationRun


def record_research_observation_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    evidence_toolset: Any | None,
    current_job_id: str | None,
):
    async def record_research_observation_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        requirement_id = str(args.get("requirement_id") or "").strip()
        if requirement_id and requirement_id not in _job_requirement_ids(job):
            return {
                "accepted": False,
                "error": "observation requirement_id must belong to the current research job",
                "requirement_id": requirement_id,
                "available_requirement_ids": sorted(_job_requirement_ids(job)),
            }
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_workspace_refs = _workspace._invalid_workspace_refs(current, source_refs)
        if invalid_workspace_refs:
            return {
                "accepted": False,
                "error": "observation source_refs must exist in the research workspace",
                "invalid_source_refs": invalid_workspace_refs,
            }
        invalid_refs = _workspace._refs_outside_job(job, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "observation source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        note = str(args.get("note") or "").strip()
        if not note:
            return {"accepted": False, "error": "note is required"}
        field_error = _workspace._semantic_field_error({"note": note}, ["note"])
        if field_error:
            return field_error
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = await _workspace._invalid_job_evidence_ids(evidence_toolset, job, evidence_ids)
        if invalid_evidence:
            return {
                "accepted": False,
                "error": "observation evidence_ids must be observed evidence from this job scope",
                "invalid_evidence_ids": invalid_evidence,
            }
        observation = {
            "requirement_id": requirement_id,
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "note": note,
            "pending_questions": [str(item).strip() for item in args.get("pending_questions") or [] if str(item).strip()],
            "created_at": _now(),
        }
        list_error = semantic_list_error(
            field="pending_questions",
            values=observation["pending_questions"],
            max_items=RESEARCH_PENDING_QUESTION_LIMIT,
            item_limit=SEMANTIC_FIELD_LIMITS["pending_question"],
        )
        if list_error:
            return list_error
        job.setdefault("observations", []).append(observation)
        if requirement_id:
            job["last_targeted_requirement_id"] = requirement_id
        job["evidence_ids"] = _dedupe([*(job.get("evidence_ids") or []), *evidence_ids])
        job["updated_at"] = _now()
        _workspace._replace_workspace_job(current, job)
        _workspace._mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "observation_count": len(job.get("observations") or []), "evidence_count": len(evidence_ids)}

    return record_research_observation_tool


def _job_requirement_ids(job: dict[str, Any]) -> set[str]:
    return {
        str(requirement.get("requirement_id") or "")
        for requirement in job.get("coverage_requirements") or []
        if isinstance(requirement, dict) and str(requirement.get("requirement_id") or "")
    }


def finish_research_job_handler(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    current_job_id: str | None,
):
    async def finish_research_job_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await _workspace.load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace._workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        status = str(args.get("status") or "").strip()
        if status not in {"mapped", "deferred"}:
            return {"accepted": False, "error": "status must be mapped or deferred"}
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"accepted": False, "error": "summary is required"}
        if status == "mapped" and not (job.get("observations") or job.get("atom_ids") or job.get("evidence_ids")):
            return {"accepted": False, "error": "mapped research job requires at least one observation, atom draft, or evidence_id"}
        if status == "deferred" and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred status"}
        deferred_reason = str(args.get("deferred_reason") or "").strip()
        planned_questions = [str(item).strip() for item in job.get("questions") or [] if str(item).strip()]
        outcomes = [dict(item) for item in args.get("question_outcomes") or [] if isinstance(item, dict)]
        outcome_error = _question_outcome_error(
            planned_questions=planned_questions,
            outcomes=outcomes,
            job_evidence_ids=[str(item) for item in job.get("evidence_ids") or []],
        )
        if outcome_error:
            return outcome_error
        requirement_outcomes = [
            dict(item) for item in args.get("requirement_outcomes") or [] if isinstance(item, dict)
        ]
        requirement_outcome_error = _requirement_outcome_error(
            workspace=current,
            job=job,
            coverage_requirements=[item for item in job.get("coverage_requirements") or [] if isinstance(item, dict)],
            outcomes=requirement_outcomes,
            job_evidence_ids=[str(item) for item in job.get("evidence_ids") or []],
            job_status=status,
        )
        if requirement_outcome_error:
            return requirement_outcome_error
        question_requirement_error = _question_requirement_outcome_error(
            job=job,
            question_outcomes=outcomes,
            requirement_outcomes=requirement_outcomes,
        )
        if question_requirement_error:
            return question_requirement_error
        field_error = _workspace._semantic_field_error(
            {"job_summary": summary, "deferred_reason": deferred_reason},
            ["job_summary", "deferred_reason"],
        )
        if field_error:
            return field_error
        job["status"] = status
        job["summary"] = summary
        job["deferred_reason"] = deferred_reason
        job["question_outcomes"] = outcomes
        job["requirement_outcomes"] = requirement_outcomes
        _apply_requirement_results(job, requirement_outcomes)
        _record_deferred_requirement_gaps(current, job, requirement_outcomes)
        job["updated_at"] = _now()
        _workspace._replace_workspace_job(current, job)
        processed = [item for item in current.get("processed_jobs") or [] if item.get("job_id") != job_id]
        processed.append({"job_id": job_id, "status": status, "summary": job["summary"], "updated_at": _now()})
        current["processed_jobs"] = processed
        current["current_job"] = None
        _workspace._reset_research_read_advisory(current)
        await _workspace.save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "status": status, "processed_job_count": len(processed)}

    return finish_research_job_tool


def _question_outcome_error(
    *,
    planned_questions: list[str],
    outcomes: list[dict[str, Any]],
    job_evidence_ids: list[str],
) -> dict[str, Any] | None:
    def key(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    planned_by_key = {key(question): question for question in planned_questions}
    outcomes_by_key: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for outcome in outcomes:
        outcome_key = key(outcome.get("question"))
        if outcome_key in outcomes_by_key:
            duplicates.append(str(outcome.get("question") or ""))
        outcomes_by_key[outcome_key] = outcome
    missing = [question for normalized, question in planned_by_key.items() if normalized not in outcomes_by_key]
    unknown = [
        str(outcome.get("question") or "")
        for normalized, outcome in outcomes_by_key.items()
        if normalized not in planned_by_key
    ]
    if missing or unknown or duplicates:
        return {
            "accepted": False,
            "error": "Every planned research question needs exactly one answered or deferred outcome",
            "missing_questions": missing,
            "unknown_questions": unknown,
            "duplicate_questions": duplicates,
        }
    allowed_evidence_ids = set(job_evidence_ids)
    for outcome in outcomes:
        outcome_status = str(outcome.get("status") or "")
        note = str(outcome.get("note") or "").strip()
        evidence_ids = _dedupe([str(item) for item in outcome.get("evidence_ids") or [] if str(item).strip()])
        if outcome_status not in {"answered", "deferred"}:
            return {"accepted": False, "error": "question outcome status must be answered or deferred"}
        if not note:
            return {"accepted": False, "error": "question outcome note is required", "question": outcome.get("question")}
        field_error = _workspace._semantic_field_error({"job_summary": note}, ["job_summary"])
        if field_error:
            return field_error
        invalid_evidence_ids = [evidence_id for evidence_id in evidence_ids if evidence_id not in allowed_evidence_ids]
        if invalid_evidence_ids:
            return {
                "accepted": False,
                "error": "question outcome evidence_ids must already belong to this research job",
                "invalid_evidence_ids": invalid_evidence_ids,
            }
        if outcome_status == "answered" and not evidence_ids:
            return {
                "accepted": False,
                "error": "answered research question requires at least one evidence_id",
                "question": outcome.get("question"),
            }
        outcome["question"] = planned_by_key[key(outcome.get("question"))]
        outcome["status"] = outcome_status
        outcome["note"] = note
        outcome["evidence_ids"] = evidence_ids
    return None


def _requirement_outcome_error(
    *,
    workspace: dict[str, Any],
    job: dict[str, Any],
    coverage_requirements: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    job_evidence_ids: list[str],
    job_status: str,
) -> dict[str, Any] | None:
    requirement_by_id = {
        str(requirement.get("requirement_id") or ""): requirement
        for requirement in coverage_requirements
        if str(requirement.get("requirement_id") or "").strip()
    }
    if not requirement_by_id:
        if outcomes:
            return {
                "accepted": False,
                "error": "requirement_outcomes must be empty when the job has no coverage_requirements",
            }
        return None
    outcomes_by_id: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for outcome in outcomes:
        requirement_id = str(outcome.get("requirement_id") or "").strip()
        if requirement_id in outcomes_by_id:
            duplicates.append(requirement_id)
        outcomes_by_id[requirement_id] = outcome
    missing = sorted(set(requirement_by_id) - set(outcomes_by_id))
    unknown = sorted(set(outcomes_by_id) - set(requirement_by_id))
    if missing or unknown or duplicates:
        return {
            "accepted": False,
            "error": "Every coverage_requirement needs exactly one answered or deferred requirement_outcome",
            "missing_requirement_ids": missing,
            "unknown_requirement_ids": unknown,
            "duplicate_requirement_ids": sorted(set(duplicates)),
        }
    deferred_requirement_ids = sorted(
        requirement_id
        for requirement_id, outcome in outcomes_by_id.items()
        if str(outcome.get("status") or "").strip() == "deferred"
    )
    if job_status == "mapped" and deferred_requirement_ids:
        return {
            "accepted": False,
            "error": "mapped research job cannot defer coverage_requirements; finish as deferred or answer them",
            "deferred_requirement_ids": deferred_requirement_ids,
        }
    if job_status == "deferred" and not deferred_requirement_ids:
        return {
            "accepted": False,
            "error": "deferred research job requires at least one deferred coverage_requirement",
        }
    allowed_evidence_ids = set(job_evidence_ids)
    active_atom_by_id = _active_job_atom_by_id(workspace, job)
    for requirement_id, outcome in outcomes_by_id.items():
        outcome_status = str(outcome.get("status") or "").strip()
        note = str(outcome.get("note") or "").strip()
        evidence_ids = _dedupe([str(item) for item in outcome.get("evidence_ids") or [] if str(item).strip()])
        supporting_atom_ids = _dedupe(
            [str(item) for item in outcome.get("supporting_atom_ids") or [] if str(item).strip()]
        )
        if outcome_status not in {"answered", "deferred"}:
            return {
                "accepted": False,
                "error": "requirement outcome status must be answered or deferred",
                "requirement_id": requirement_id,
            }
        if not note:
            return {
                "accepted": False,
                "error": "requirement outcome note is required",
                "requirement_id": requirement_id,
            }
        field_error = _workspace._semantic_field_error({"job_summary": note}, ["job_summary"])
        if field_error:
            return field_error
        invalid_evidence_ids = [evidence_id for evidence_id in evidence_ids if evidence_id not in allowed_evidence_ids]
        if invalid_evidence_ids:
            return {
                "accepted": False,
                "error": "requirement outcome evidence_ids must already belong to this research job",
                "requirement_id": requirement_id,
                "invalid_evidence_ids": invalid_evidence_ids,
            }
        if outcome_status == "answered" and not evidence_ids:
            return {
                "accepted": False,
                "error": "answered requirement requires at least one evidence_id",
                "requirement_id": requirement_id,
            }
        invalid_supporting_atom_ids = [
            atom_id for atom_id in supporting_atom_ids if atom_id not in active_atom_by_id
        ]
        if invalid_supporting_atom_ids:
            return {
                "accepted": False,
                "error": "requirement outcome supporting_atom_ids must be active atoms from this research job",
                "requirement_id": requirement_id,
                "invalid_supporting_atom_ids": invalid_supporting_atom_ids,
                "available_atom_ids": sorted(active_atom_by_id),
            }
        if outcome_status == "answered" and not supporting_atom_ids:
            return {
                "accepted": False,
                "error": "answered requirement requires at least one supporting_atom_id",
                "requirement_id": requirement_id,
                "available_atom_ids": sorted(active_atom_by_id),
            }
        atoms_without_shared_evidence = [
            atom_id
            for atom_id in supporting_atom_ids
            if not (set(active_atom_by_id[atom_id].get("evidence_ids") or []) & set(evidence_ids))
        ]
        if outcome_status == "answered" and atoms_without_shared_evidence:
            return {
                "accepted": False,
                "error": "answered requirement supporting_atom_ids must share evidence_ids with the requirement outcome",
                "requirement_id": requirement_id,
                "supporting_atom_ids_without_shared_evidence": atoms_without_shared_evidence,
            }
        outcome["requirement_id"] = requirement_id
        outcome["status"] = outcome_status
        outcome["note"] = note
        outcome["evidence_ids"] = evidence_ids
        outcome["supporting_atom_ids"] = supporting_atom_ids
    return None


def _active_job_atom_by_id(workspace: dict[str, Any], job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    job_id = str(job.get("job_id") or "")
    job_atom_ids = {str(item) for item in job.get("atom_ids") or [] if str(item).strip()}
    values: dict[str, dict[str, Any]] = {}
    for atom in _active_atom_drafts(workspace.get("atom_drafts") or []):
        atom_id = str(atom.get("atom_id") or "")
        if not atom_id or atom_id not in job_atom_ids:
            continue
        if job_id and str(atom.get("job_id") or "") != job_id:
            continue
        values[atom_id] = atom
    return values


def _apply_requirement_results(job: dict[str, Any], requirement_outcomes: list[dict[str, Any]]) -> None:
    outcome_by_id = {
        str(outcome.get("requirement_id") or ""): outcome
        for outcome in requirement_outcomes
        if str(outcome.get("requirement_id") or "")
    }
    updated_requirements: list[dict[str, Any]] = []
    for requirement in job.get("coverage_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        requirement = dict(requirement)
        outcome = outcome_by_id.get(str(requirement.get("requirement_id") or ""))
        if outcome is not None:
            requirement["result"] = {
                **dict(requirement.get("result") or {}),
                "status": outcome["status"],
                "summary": outcome["note"],
                "evidence_ids": list(outcome.get("evidence_ids") or []),
                "supporting_atom_ids": list(outcome.get("supporting_atom_ids") or []),
            }
        updated_requirements.append(requirement)
    job["coverage_requirements"] = updated_requirements
    job["open_requirements"] = [
        requirement
        for requirement in updated_requirements
        if str((requirement.get("result") or {}).get("status") or "") != "answered"
    ]


def _question_requirement_outcome_error(
    *,
    job: dict[str, Any],
    question_outcomes: list[dict[str, Any]],
    requirement_outcomes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    question_ids = [str(item) for item in job.get("coverage_question_ids") or [] if str(item)]
    questions = [str(item).strip() for item in job.get("questions") or [] if str(item).strip()]
    question_by_id = {
        question_id: questions[index]
        for index, question_id in enumerate(question_ids)
        if index < len(questions)
    }
    single_question_fallback_id = ""
    if len(questions) == 1:
        single_question_fallback_id = question_ids[0] if len(question_ids) == 1 else "__single_question__"
        question_by_id.setdefault(single_question_fallback_id, questions[0])
    requirement_status_by_id = {
        str(outcome.get("requirement_id") or ""): str(outcome.get("status") or "")
        for outcome in requirement_outcomes
        if str(outcome.get("requirement_id") or "")
    }
    requirement_statuses_by_question: dict[str, list[str]] = {}
    for requirement in job.get("coverage_requirements") or []:
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("requirement_id") or "")
        question_id = str(requirement.get("question_id") or "")
        status = requirement_status_by_id.get(requirement_id, "")
        if not status:
            continue
        target_question_id = question_id if question_id in question_by_id else single_question_fallback_id
        if target_question_id:
            requirement_statuses_by_question.setdefault(target_question_id, []).append(status)
    deferred_requirement_ids = {
        requirement_id
        for requirement_id, status in requirement_status_by_id.items()
        if status == "deferred"
    }
    deferred_questions = {
        question_by_id[question_id]
        for question_id, statuses in requirement_statuses_by_question.items()
        if question_id in question_by_id and "deferred" in statuses
    }
    answered_deferred_questions = [
        str(outcome.get("question") or "")
        for outcome in question_outcomes
        if str(outcome.get("question") or "") in deferred_questions
        and str(outcome.get("status") or "") == "answered"
    ]
    if answered_deferred_questions:
        return {
            "accepted": False,
            "error": "question_outcome cannot be answered while one of its coverage_requirements is deferred",
            "answered_questions_with_deferred_requirements": answered_deferred_questions,
            "deferred_requirement_ids": sorted(deferred_requirement_ids),
        }
    fully_answered_questions = {
        question_by_id[question_id]
        for question_id, statuses in requirement_statuses_by_question.items()
        if question_id in question_by_id and statuses and set(statuses) == {"answered"}
    }
    deferred_answered_questions = [
        str(outcome.get("question") or "")
        for outcome in question_outcomes
        if str(outcome.get("question") or "") in fully_answered_questions
        and str(outcome.get("status") or "") == "deferred"
    ]
    if deferred_answered_questions:
        return {
            "accepted": False,
            "error": "question_outcome cannot be deferred when all of its coverage_requirements are answered",
            "deferred_questions_with_answered_requirements": deferred_answered_questions,
        }
    return None


def _record_deferred_requirement_gaps(
    workspace: dict[str, Any],
    job: dict[str, Any],
    requirement_outcomes: list[dict[str, Any]],
) -> None:
    requirement_by_id = {
        str(requirement.get("requirement_id") or ""): requirement
        for requirement in job.get("coverage_requirements") or []
        if isinstance(requirement, dict) and str(requirement.get("requirement_id") or "")
    }
    current_gaps = [item for item in workspace.get("research_gaps") or [] if isinstance(item, dict)]
    gaps_by_key = {
        str(item.get("normalized_key") or _gap_key(item.get("category") or "", item.get("question") or "")): dict(item)
        for item in current_gaps
    }
    for outcome in requirement_outcomes:
        requirement = requirement_by_id.get(str(outcome.get("requirement_id") or "")) or {}
        question = (
            str(requirement.get("description") or "").strip()
            or str(outcome.get("note") or "").strip()
            or str(outcome.get("requirement_id") or "").strip()
        )
        normalized_key = _requirement_gap_key(job, requirement, outcome, question)
        existing = gaps_by_key.get(normalized_key)
        if str(outcome.get("status") or "") == "answered" and existing is not None:
            gaps_by_key[normalized_key] = {
                **existing,
                "status": "resolved",
                "resolution_note": str(outcome.get("note") or ""),
                "evidence_ids": _dedupe(
                    [
                        *[str(item) for item in existing.get("evidence_ids") or []],
                        *[str(item) for item in outcome.get("evidence_ids") or []],
                    ]
                ),
                "updated_at": _now(),
            }
            continue
        if str(outcome.get("status") or "") != "deferred":
            continue
        treatment_relevance = (
            "central"
            if str(job.get("priority") or "").strip().lower() in {"critical", "high"}
            else "supporting"
        )
        gap = {
            "gap_id": str((existing or {}).get("gap_id") or _stable_id(normalized_key, question, len(gaps_by_key) + 1)),
            "job_id": str(job.get("job_id") or ""),
            "question_id": str(requirement.get("question_id") or ""),
            "requirement_id": str(outcome.get("requirement_id") or ""),
            "question": question,
            "category": "open_requirement",
            "severity": "non_blocking",
            "treatment_relevance": treatment_relevance,
            "source_refs": _dedupe(
                [
                    *[str(item) for item in (existing or {}).get("source_refs") or []],
                    *[str(item) for item in job.get("source_refs") or []],
                ]
            ),
            "evidence_ids": _dedupe(
                [
                    *[str(item) for item in (existing or {}).get("evidence_ids") or []],
                    *[str(item) for item in outcome.get("evidence_ids") or []],
                ]
            ),
            "atom_refs": [str(item) for item in (existing or {}).get("atom_refs") or []],
            "status": "deferred",
            "resolution_note": str(outcome.get("note") or ""),
            "normalized_key": normalized_key,
            "created_at": str((existing or {}).get("created_at") or _now()),
            "updated_at": _now(),
        }
        gaps_by_key[normalized_key] = gap
    workspace["research_gaps"] = list(gaps_by_key.values())


def _requirement_gap_key(
    job: dict[str, Any],
    requirement: dict[str, Any],
    outcome: dict[str, Any],
    question: str,
) -> str:
    del question
    identity = ":".join(
        [
            str(job.get("job_id") or ""),
            str(requirement.get("question_id") or ""),
            str(outcome.get("requirement_id") or ""),
        ]
    )
    return _gap_key("open_requirement", identity)
