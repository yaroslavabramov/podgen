from __future__ import annotations

from typing import Any

from app.generation_v2.research_source_map_prompting import source_map_summary_for_job
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_READS_PER_COMMIT_ADVISORY,
    RESEARCH_READS_PER_COMMIT_WARNING,
    SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.research_workspace_helpers import (
    active_atom_drafts as _active_atom_drafts,
    active_job_atom_count as _active_job_atom_count,
    story_weight as _story_weight,
)
from app.generation_v2.research_workspace_gap_helpers import (
    dedupe_gaps as _dedupe_gaps,
    treatment_relevance as _treatment_relevance,
)


def _preview_fields(name: str, value: Any, limit: int) -> dict[str, Any]:
    text = str(value or "")
    return {
        f"{name}_preview": text[:limit],
        f"{name}_chars": len(text),
        f"{name}_truncated": len(text) > limit,
    }


def _page_metadata(*, total: int, cursor: int, returned: int) -> dict[str, Any]:
    next_offset = cursor + returned
    return {
        "total_count": total,
        "returned_count": returned,
        "omitted_count": max(0, total - next_offset),
        "cursor": str(cursor),
        "next_cursor": str(next_offset) if next_offset < total else "",
    }


def prompt_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(workspace or {})
    workspace.setdefault("source_order", [])
    workspace.setdefault("investigation_jobs", [])
    workspace.setdefault("processed_jobs", [])
    workspace.setdefault("covered_source_refs", [])
    workspace.setdefault("selected_evidence_ids", [])
    workspace.setdefault("atom_drafts", [])
    workspace.setdefault("research_gaps", [])
    workspace.setdefault("risk_register", [])
    return workspace


def workspace_job(workspace: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    for item in workspace.get("investigation_jobs") or []:
        if item.get("job_id") == job_id:
            return dict(item)
    return None


def prompt_gap_payload(gap: dict[str, Any], *, include_evidence_ids: bool) -> dict[str, Any]:
    evidence_ids = gap.get("evidence_ids") or []
    payload = {
        "gap_id": gap.get("gap_id"),
        **_preview_fields("question", gap.get("question") or "", 600),
        "category": gap.get("category"),
        "severity": gap.get("severity"),
        "treatment_relevance": _treatment_relevance(gap.get("treatment_relevance")),
        "status": gap.get("status"),
        "source_refs": gap.get("source_refs") or [],
        "atom_refs": gap.get("atom_refs") or [],
        "evidence_count": len(evidence_ids),
        "has_evidence": bool(evidence_ids),
        **_preview_fields("resolution_note", gap.get("resolution_note") or "", 500),
    }
    if include_evidence_ids and evidence_ids:
        payload["evidence_ids"] = evidence_ids
    return payload


def prompt_atom_payload(atom: dict[str, Any], *, include_evidence_ids: bool, atom_ref: str | None = None) -> dict[str, Any]:
    evidence_ids = atom.get("evidence_ids") or []
    payload = {
        "atom_ref": atom_ref,
        "atom_id": atom.get("atom_id"),
        "job_id": atom.get("job_id"),
        "kind": atom.get("kind"),
        "title": atom.get("title"),
        **_preview_fields("claim", atom.get("claim") or "", 700),
        **_preview_fields("explanation", atom.get("explanation") or "", 900),
        **_preview_fields("story_value", atom.get("story_value") or "", 500),
        "source_refs": atom.get("source_refs") or [],
        "evidence_count": len(evidence_ids),
        "has_evidence": bool(evidence_ids),
        "confidence": atom.get("confidence"),
        "risk": atom.get("risk"),
        "source_coverage_status": atom.get("source_coverage_status"),
        "story_weight": _story_weight(str(atom.get("story_weight") or ""), kind=str(atom.get("kind") or "")),
        **_preview_fields("treatment_role_hint", atom.get("treatment_role_hint") or "", 400),
    }
    if include_evidence_ids and evidence_ids:
        payload["evidence_ids"] = evidence_ids
    return payload


def _job_question_page(job: dict[str, Any], *, cursor: int, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    questions = [str(item).strip() for item in job.get("questions") or [] if str(item).strip()]
    page = questions[cursor : cursor + limit]
    items = [
        {
            "question_number": cursor + idx,
            **_preview_fields("question", question, SEMANTIC_FIELD_LIMITS["question"]),
        }
        for idx, question in enumerate(page, start=1)
    ]
    return items, _page_metadata(total=len(questions), cursor=cursor, returned=len(items))


def _job_observation_page(
    job: dict[str, Any], *, cursor: int, limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observations = [item for item in job.get("observations") or [] if isinstance(item, dict)]
    page = observations[cursor : cursor + limit]
    items = [
        {
            "observation_number": cursor + idx,
            "source_refs": item.get("source_refs") or [],
            "evidence_ids": item.get("evidence_ids") or [],
            "evidence_count": len(item.get("evidence_ids") or []),
            **_preview_fields("note", item.get("note") or "", 700),
            "pending_question_previews": [
                _preview_fields("text", pending, SEMANTIC_FIELD_LIMITS["pending_question"])
                for pending in item.get("pending_questions") or []
            ],
            "created_at": item.get("created_at"),
        }
        for idx, item in enumerate(page, start=1)
    ]
    return items, _page_metadata(total=len(observations), cursor=cursor, returned=len(items))


def current_job_prompt_payload(
    job: dict[str, Any],
    *,
    question_cursor: int = 0,
    max_questions: int = 6,
    observation_cursor: int = 0,
    max_observations: int = 12,
) -> dict[str, Any]:
    questions, question_page = _job_question_page(
        job,
        cursor=max(0, question_cursor),
        limit=max(1, max_questions),
    )
    observations, observation_page = _job_observation_page(
        job,
        cursor=max(0, observation_cursor),
        limit=max(1, max_observations),
    )
    repair_gap = job.get("repair_gap") if isinstance(job.get("repair_gap"), dict) else None
    payload: dict[str, Any] = {
        "job_id": job.get("job_id"),
        **_preview_fields("label", job.get("label") or "", SEMANTIC_FIELD_LIMITS["label"]),
        "story_job": job.get("story_job"),
        **_preview_fields("purpose", job.get("purpose") or "", SEMANTIC_FIELD_LIMITS["purpose"]),
        "source_refs": job.get("source_refs") or [],
        "required_atom_kinds": job.get("required_atom_kinds") or [],
        "priority": job.get("priority"),
        "allow_broad_search": bool(job.get("allow_broad_search")),
        "status": job.get("status"),
        "attempt": int(job.get("attempt") or 0),
        "questions": questions,
        "observations": observations,
        "question_outcomes": [
            {
                **_preview_fields(
                    "question",
                    item.get("question") or "",
                    SEMANTIC_FIELD_LIMITS["question"],
                ),
                "status": item.get("status"),
                **_preview_fields("note", item.get("note") or "", 700),
                "evidence_ids": item.get("evidence_ids") or [],
            }
            for item in job.get("question_outcomes") or []
            if isinstance(item, dict)
        ],
        "evidence_count": len(job.get("evidence_ids") or []),
        "atom_ids": job.get("atom_ids") or [],
        "atom_count": len(job.get("atom_ids") or []),
        **_preview_fields("summary", job.get("summary") or "", 700),
        **_preview_fields("deferred_reason", job.get("deferred_reason") or "", 500),
        **_preview_fields("resume_reason", job.get("resume_reason") or "", 500),
        "repair_for_gap_id": job.get("repair_for_gap_id"),
        "pages": {
            "questions": question_page,
            "observations": observation_page,
        },
    }
    if repair_gap is not None:
        payload["repair_gap"] = prompt_gap_payload(repair_gap, include_evidence_ids=True)
    return payload


def research_workspace_summary_for_prompt(
    workspace: dict[str, Any],
    *,
    max_jobs: int = 30,
    max_atoms: int = 30,
    max_gaps: int = 80,
    job_cursor: int = 0,
    atom_cursor: int = 0,
    gap_cursor: int = 0,
    risk_cursor: int = 0,
    audit_feedback_cursor: int = 0,
    max_risks: int = 30,
    max_audit_feedback: int = 30,
    include_atom_evidence_ids: bool = True,
    include_gap_evidence_ids: bool = True,
) -> dict[str, Any]:
    workspace = prompt_workspace(workspace)
    jobs = list(workspace.get("investigation_jobs") or [])
    atoms = _active_atom_drafts(workspace.get("atom_drafts") or [])
    gaps = _dedupe_gaps(workspace.get("research_gaps") or [])
    risks = list(workspace.get("risk_register") or [])
    audit_feedback = [
        item for item in workspace.get("audit_repair_feedback") or [] if isinstance(item, dict)
    ]
    job_page = jobs[job_cursor : job_cursor + max_jobs]
    atom_page = atoms[atom_cursor : atom_cursor + max_atoms]
    gap_page = gaps[gap_cursor : gap_cursor + max_gaps]
    audit_feedback_page = audit_feedback[
        audit_feedback_cursor : audit_feedback_cursor + max_audit_feedback
    ]
    return {
        "status": workspace.get("status"),
        "source_count": len(workspace.get("source_order") or []),
        "job_count": len(workspace.get("investigation_jobs") or []),
        "current_job": workspace.get("current_job"),
        "processed_jobs": [
            {
                "job_id": item.get("job_id"),
                "status": item.get("status"),
                **_preview_fields("summary", item.get("summary") or "", 500),
            }
            for item in workspace.get("processed_jobs") or []
            if isinstance(item, dict)
        ],
        "covered_source_refs": workspace.get("covered_source_refs") or [],
        "selected_evidence_count": len(workspace.get("selected_evidence_ids") or []),
        "research_gaps": [
            prompt_gap_payload(item, include_evidence_ids=include_gap_evidence_ids)
            for item in gap_page
        ],
        "risk_count": len(risks),
        "jobs": [
            {
                "job_id": item.get("job_id"),
                "label": item.get("label"),
                "story_job": item.get("story_job"),
                "source_refs": item.get("source_refs") or [],
                "status": item.get("status"),
                "priority": item.get("priority"),
                "question_count": len(item.get("questions") or []),
                "observation_count": len(item.get("observations") or []),
                "atom_count": _active_job_atom_count(workspace, item),
                **_preview_fields("summary", item.get("summary") or "", 500),
                **_preview_fields("deferred_reason", item.get("deferred_reason") or "", 400),
            }
            for item in job_page
        ],
        "atom_drafts": [
            prompt_atom_payload(item, include_evidence_ids=include_atom_evidence_ids, atom_ref=f"A{idx:02d}")
            for idx, item in enumerate(atom_page, start=atom_cursor + 1)
        ],
        "pages": {
            "jobs": _page_metadata(total=len(jobs), cursor=job_cursor, returned=len(job_page)),
            "atom_drafts": _page_metadata(total=len(atoms), cursor=atom_cursor, returned=len(atom_page)),
            "research_gaps": _page_metadata(total=len(gaps), cursor=gap_cursor, returned=len(gap_page)),
            "workspace_risks": _page_metadata(total=len(risks), cursor=risk_cursor, returned=0),
            "audit_feedback": _page_metadata(
                total=len(audit_feedback),
                cursor=audit_feedback_cursor,
                returned=len(audit_feedback_page),
            ),
        },
        "audit_repair_feedback": [
            {
                "target_step": item.get("target_step"),
                "severity": item.get("severity"),
                **_preview_fields("description", item.get("description") or "", 600),
                **_preview_fields("suggested_repair", item.get("suggested_repair") or "", 600),
                "atom_refs": item.get("atom_refs") or [],
            }
            for item in audit_feedback_page
        ],
        "audit_repair_feedback_count": len(audit_feedback),
        "commit_advisory": {
            "reads_since_commit": workspace.get("reads_since_commit") or 0,
            "warning_threshold": RESEARCH_READS_PER_COMMIT_WARNING,
            "advisory_threshold": RESEARCH_READS_PER_COMMIT_ADVISORY,
            "commit_recommended": bool(workspace.get("commit_recommended")),
            "next_action_hint": (
                "If reads_since_commit is near the warning threshold, persist progress with "
                "record_research_observation, upsert_editorial_atom, record_research_risk, or record_research_gap."
            ),
        },
    }


def research_finalizer_payload_for_prompt(
    workspace: dict[str, Any],
    *,
    max_jobs: int = 60,
    max_atoms: int = 60,
    max_observations: int = 120,
    max_risks: int = 30,
    observation_cursor: int = 0,
    risk_cursor: int = 0,
) -> dict[str, Any]:
    workspace = prompt_workspace(workspace)
    observations: list[dict[str, Any]] = []
    for job in workspace.get("investigation_jobs") or []:
        for observation in job.get("observations") or []:
            evidence_ids = observation.get("evidence_ids") or []
            observations.append(
                {
                    "job_id": job.get("job_id"),
                    "job_label": job.get("label"),
                    "source_refs": observation.get("source_refs") or [],
                    "evidence_count": len(evidence_ids),
                    "has_evidence": bool(evidence_ids),
                    **_preview_fields("note", observation.get("note") or "", 700),
                    "pending_question_previews": [
                        _preview_fields("text", item, 220) for item in observation.get("pending_questions") or []
                    ],
                }
            )
    base = research_workspace_summary_for_prompt(
        workspace,
        max_jobs=max_jobs,
        max_atoms=max_atoms,
        max_risks=max_risks,
        risk_cursor=risk_cursor,
        include_atom_evidence_ids=False,
        include_gap_evidence_ids=False,
    )
    observation_page = observations[observation_cursor : observation_cursor + max_observations]
    risks = list(workspace.get("risk_register") or [])
    risk_page = risks[risk_cursor : risk_cursor + max_risks]
    return {
        "status": base.get("status"),
        "source_count": base.get("source_count"),
        "job_count": base.get("job_count"),
        "selected_evidence_count": base.get("selected_evidence_count"),
        "jobs": base.get("jobs") or [],
        "atom_drafts": base.get("atom_drafts") or [],
        "observations": observation_page,
        "research_gaps": base.get("research_gaps") or [],
        "risk_register": [
            {
                **_preview_fields("risk", item.get("risk") if isinstance(item, dict) else item, 500),
                "severity": str(item.get("severity") or "medium") if isinstance(item, dict) else "medium",
                "source_refs": (item.get("source_refs") or []) if isinstance(item, dict) else [],
                "atom_refs": (item.get("atom_refs") or []) if isinstance(item, dict) else [],
                "evidence_count": len(item.get("evidence_ids") or []) if isinstance(item, dict) else 0,
                "has_evidence": bool(item.get("evidence_ids") or []) if isinstance(item, dict) else False,
            }
            for item in risk_page
        ],
        "pages": {
            **(base.get("pages") or {}),
            "observations": _page_metadata(
                total=len(observations), cursor=observation_cursor, returned=len(observation_page)
            ),
            "workspace_risks": _page_metadata(
                total=len(risks), cursor=risk_cursor, returned=len(risk_page)
            ),
        },
        "audit_repair_feedback": base.get("audit_repair_feedback") or [],
        "assembly_instruction": (
            "Use atom_drafts as the memo backbone, but treat them as raw outputs from independent jobs that "
            "may overlap. Audit same-kind atom_drafts for repeated findings before finalization and use "
            "merge_editorial_atoms for duplicates, passing atom_id handles. Use atom_ref handles for risk/gap "
            "links. Use observations and research_gaps to explain coverage and risks. Do not invent atoms "
            "that were not persisted as atom_drafts. Blocking open/deferred gaps become final open_questions; "
            "non-blocking/optional gaps become risk/source-coverage notes."
        ),
    }


def research_job_prompt_payload(workspace: dict[str, Any], job_id: str, *, source_map_summary: dict[str, Any]) -> dict[str, Any]:
    workspace = prompt_workspace(workspace)
    job = workspace_job(workspace, job_id) or {}
    return {
        "job": current_job_prompt_payload(job),
        "workspace_progress": research_workspace_summary_for_prompt(
            workspace,
            max_jobs=40,
            max_atoms=40,
            include_atom_evidence_ids=False,
            include_gap_evidence_ids=False,
        ),
        "source_map_summary": source_map_summary_for_job(source_map_summary, job),
        "commit_advisory": {
            "reads_since_commit": workspace.get("reads_since_commit") or 0,
            "warning_threshold": RESEARCH_READS_PER_COMMIT_WARNING,
            "advisory_threshold": RESEARCH_READS_PER_COMMIT_ADVISORY,
            "commit_recommended": bool(workspace.get("commit_recommended")),
        },
    }


__all__ = [
    "current_job_prompt_payload",
    "prompt_atom_payload",
    "prompt_gap_payload",
    "prompt_workspace",
    "research_finalizer_payload_for_prompt",
    "research_job_prompt_payload",
    "research_workspace_summary_for_prompt",
    "workspace_job",
]
