from __future__ import annotations

from typing import Any

from app.generation_v2.research_source_map_prompting import source_map_summary_for_job
from app.generation_v2.model_context_text import model_preview_text
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
    text = model_preview_text(value, collapse_whitespace=False)
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
    workspace.setdefault("corpus_brief_preview", None)
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


def prompt_atom_payload(
    atom: dict[str, Any], *, include_evidence_ids: bool, atom_ref: str | None = None
) -> dict[str, Any]:
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


def prompt_atom_index_payload(atom: dict[str, Any], *, atom_ref: str) -> dict[str, Any]:
    """Compact routing entry; full semantic fields stay available through atom_drafts paging."""

    evidence_ids = atom.get("evidence_ids") or []
    return {
        "atom_ref": atom_ref,
        "atom_id": atom.get("atom_id"),
        "job_id": atom.get("job_id"),
        "kind": atom.get("kind"),
        **_preview_fields("title", atom.get("title") or "", 220),
        **_preview_fields("claim", atom.get("claim") or "", 360),
        **_preview_fields("story_value", atom.get("story_value") or "", 240),
        "source_refs": atom.get("source_refs") or [],
        "evidence_count": len(evidence_ids),
        "confidence": atom.get("confidence"),
        "risk": atom.get("risk"),
        "source_coverage_status": atom.get("source_coverage_status"),
        "story_weight": _story_weight(str(atom.get("story_weight") or ""), kind=str(atom.get("kind") or "")),
    }


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
    coverage_question_ids = [
        str(item) for item in job.get("coverage_question_ids") or [] if str(item).strip()
    ]
    brief_outcomes = [
        item for item in job.get("corpus_brief_outcomes") or [] if isinstance(item, dict)
    ]
    brief_outcome_previews = [
        {
            "question_id": item.get("question_id"),
            "status": item.get("status"),
            "next_action": item.get("next_action"),
            "source_refs": item.get("source_refs") or [],
            "evidence_count": len(item.get("evidence_ids") or []),
            "finding_ids": [str(value) for value in item.get("finding_ids") or []][:12],
            "finding_count": len(item.get("finding_ids") or []),
            "requirement_result_count": len(item.get("requirement_results") or []),
            "semantic_revision": int(item.get("semantic_revision") or 0),
            "is_stale": bool(item.get("is_stale")),
            **_preview_fields("orientation_summary", item.get("orientation_summary") or "", 600),
            **_preview_fields("missing_detail", item.get("missing_detail") or "", 500),
            **_preview_fields("conflict_summary", item.get("conflict_summary") or "", 500),
            **_preview_fields("deferred_reason", item.get("deferred_reason") or "", 400),
            "confidence": item.get("confidence"),
        }
        for item in brief_outcomes
    ]
    coverage_requirements = [
        item for item in job.get("coverage_requirements") or [] if isinstance(item, dict)
    ]
    requirement_previews = [
        {
            "question_id": item.get("question_id"),
            "requirement_id": item.get("requirement_id"),
            **_preview_fields("description", item.get("description") or "", 600),
            **_preview_fields("rationale", item.get("rationale") or "", 400),
            "result": {
                "status": (item.get("result") or {}).get("status"),
                "finding_ids": [
                    str(value) for value in (item.get("result") or {}).get("finding_ids") or []
                ][:12],
                "finding_count": len((item.get("result") or {}).get("finding_ids") or []),
                **_preview_fields(
                    "summary", (item.get("result") or {}).get("summary") or "", 500
                ),
                **_preview_fields(
                    "missing_detail",
                    (item.get("result") or {}).get("missing_detail") or "",
                    400,
                ),
                **_preview_fields(
                    "conflict_summary",
                    (item.get("result") or {}).get("conflict_summary") or "",
                    400,
                ),
            },
        }
        for item in coverage_requirements[:12]
    ]
    source_findings = [
        item for item in job.get("source_findings") or [] if isinstance(item, dict)
    ]
    finding_previews = [
        {
            "finding_id": item.get("finding_id"),
            "source_ref": item.get("source_ref"),
            "evidence_count": len(item.get("evidence_ids") or []),
            "semantic_revision": int(item.get("semantic_revision") or 0),
            **_preview_fields("statement", item.get("statement") or "", 700),
            "scope": {
                **_preview_fields("actor", (item.get("scope") or {}).get("actor") or "", 200),
                **_preview_fields("period", (item.get("scope") or {}).get("period") or "", 200),
                **_preview_fields(
                    "geography", (item.get("scope") or {}).get("geography") or "", 200
                ),
                "qualifier_count": len((item.get("scope") or {}).get("qualifiers") or []),
            },
            "question_links": [
                {
                    "question_id": link.get("question_id"),
                    "requirement_ids": link.get("requirement_ids") or [],
                }
                for link in item.get("question_links") or []
                if isinstance(link, dict)
            ],
            "limitation_count": len(item.get("limitations") or []),
        }
        for item in source_findings[:12]
    ]
    starting_evidence_ids = [str(item) for item in job.get("starting_evidence_ids") or [] if str(item).strip()]
    starting_evidence_page = starting_evidence_ids[:12]
    payload: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "corpus_handoff_id": job.get("corpus_handoff_id"),
        "coverage_question_ids": coverage_question_ids,
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
        "starting_evidence_ids": starting_evidence_page,
        "starting_evidence_count": len(starting_evidence_ids),
        "evidence_count": len(job.get("evidence_ids") or []),
        "atom_ids": job.get("atom_ids") or [],
        "atom_count": len(job.get("atom_ids") or []),
        **_preview_fields("summary", job.get("summary") or "", 700),
        **_preview_fields("deferred_reason", job.get("deferred_reason") or "", 500),
        **_preview_fields("resume_reason", job.get("resume_reason") or "", 500),
        "repair_for_gap_id": job.get("repair_for_gap_id"),
        "corpus_brief_lineage": {
            "handoff_question_assignments": job.get("corpus_handoff_question_assignments") or [],
            "handoff_finding_ids": job.get("corpus_handoff_finding_ids") or [],
            "outcomes": brief_outcome_previews,
            "coverage_requirement_previews": requirement_previews,
            "coverage_requirement_count": len(coverage_requirements),
            "coverage_requirement_omitted_count": max(
                0, len(coverage_requirements) - len(requirement_previews)
            ),
            "open_requirement_count": len(job.get("open_requirements") or []),
            "source_finding_previews": finding_previews,
            "source_finding_count": len(source_findings),
            "source_finding_omitted_count": max(
                0, len(source_findings) - len(finding_previews)
            ),
            "requires_full_outcome_read": any(
                str(item.get("status") or "") in {"partial", "conflicted"}
                for item in brief_outcomes
            ),
            "paging_instruction": (
                "Page corpus_brief_questions, corpus_brief_findings, and corpus_brief_outcomes from "
                "cursor 0 until each next_cursor is empty whenever the counts above exceed the previews "
                "or a preview is truncated. Preserve requirements, finding lineage, missing details, "
                "and conflicts when investigating."
            ),
        },
        "pages": {
            "questions": question_page,
            "observations": observation_page,
            "coverage_requirements": {
                **_page_metadata(
                    total=len(coverage_requirements),
                    cursor=0,
                    returned=len(requirement_previews),
                ),
                "cursor_unit": "job_preview_record",
                "full_section": "corpus_brief_questions",
                "full_page_start_cursor": "0",
                "full_page_unit": "record_part",
            },
            "source_findings": {
                **_page_metadata(
                    total=len(source_findings),
                    cursor=0,
                    returned=len(finding_previews),
                ),
                "cursor_unit": "job_preview_record",
                "full_section": "corpus_brief_findings",
                "full_page_start_cursor": "0",
                "full_page_unit": "record_part",
            },
            "starting_evidence": _page_metadata(
                total=len(starting_evidence_ids),
                cursor=0,
                returned=len(starting_evidence_page),
            ),
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
    audit_feedback = [item for item in workspace.get("audit_repair_feedback") or [] if isinstance(item, dict)]
    job_page = jobs[job_cursor : job_cursor + max_jobs]
    atom_page = atoms[atom_cursor : atom_cursor + max_atoms]
    gap_page = gaps[gap_cursor : gap_cursor + max_gaps]
    audit_feedback_page = audit_feedback[audit_feedback_cursor : audit_feedback_cursor + max_audit_feedback]
    return {
        "status": workspace.get("status"),
        "corpus_brief_attached": bool(workspace.get("corpus_brief_artifact_id")),
        "corpus_brief_preview": workspace.get("corpus_brief_preview"),
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
        "research_gaps": [prompt_gap_payload(item, include_evidence_ids=include_gap_evidence_ids) for item in gap_page],
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
    max_observations: int = 0,
    max_gaps: int = 40,
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
    atoms = _active_atom_drafts(workspace.get("atom_drafts") or [])
    gaps = _dedupe_gaps(workspace.get("research_gaps") or [])
    safe_max_atoms = max(0, max_atoms)
    safe_max_gaps = max(0, max_gaps)
    atom_page = atoms[:safe_max_atoms]
    gap_page = gaps[:safe_max_gaps]
    base = research_workspace_summary_for_prompt(
        workspace,
        max_jobs=max_jobs,
        max_atoms=0,
        max_gaps=0,
        max_risks=max_risks,
        risk_cursor=risk_cursor,
        include_atom_evidence_ids=False,
        include_gap_evidence_ids=False,
    )
    observation_limit = max(0, max_observations)
    observation_page = observations[observation_cursor : observation_cursor + observation_limit]
    risks = list(workspace.get("risk_register") or [])
    risk_page = risks[risk_cursor : risk_cursor + max_risks]
    return {
        "status": base.get("status"),
        "source_count": base.get("source_count"),
        "job_count": base.get("job_count"),
        "selected_evidence_count": base.get("selected_evidence_count"),
        "jobs": base.get("jobs") or [],
        "atom_index": [
            prompt_atom_index_payload(item, atom_ref=f"A{idx:02d}")
            for idx, item in enumerate(atom_page, start=1)
        ],
        "observations": observation_page,
        "research_gaps": [prompt_gap_payload(item, include_evidence_ids=False) for item in gap_page],
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
            "atom_index": _page_metadata(total=len(atoms), cursor=0, returned=len(atom_page)),
            "research_gaps": _page_metadata(total=len(gaps), cursor=0, returned=len(gap_page)),
            "observations": _page_metadata(
                total=len(observations), cursor=observation_cursor, returned=len(observation_page)
            ),
            "workspace_risks": _page_metadata(total=len(risks), cursor=risk_cursor, returned=len(risk_page)),
        },
        "audit_repair_feedback": base.get("audit_repair_feedback") or [],
        "assembly_instruction": (
            "atom_index is the compact index of active findings. Page atom_index until next_cursor is empty, "
            "then scan every entry for overlap. Use list_research_workspace_items(section=atom_drafts, "
            "cursor=<Axx number minus one>) only for same-kind entries that may express the same finding, "
            "then merge confirmed duplicates. Page "
            "observations only when an atom, gap, or risk cannot be understood from its index entry. Do not "
            "invent atoms that were not persisted. Blocking open/deferred gaps become final open_questions; "
            "non-blocking/optional gaps become risk/source-coverage notes."
        ),
    }


def research_job_workspace_progress_for_prompt(
    workspace: dict[str, Any],
    *,
    current_job_id: str,
) -> dict[str, Any]:
    """Return routing state, not other agents' semantic work."""

    workspace = prompt_workspace(workspace)
    jobs = [item for item in workspace.get("investigation_jobs") or [] if isinstance(item, dict)]
    atoms = _active_atom_drafts(workspace.get("atom_drafts") or [])
    gaps = _dedupe_gaps(workspace.get("research_gaps") or [])
    status_counts: dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "status": workspace.get("status"),
        "job_count": len(jobs),
        "job_status_counts": status_counts,
        "current_job_id": current_job_id,
        "other_job_index": [
            {
                "job_id": item.get("job_id"),
                **_preview_fields("label", item.get("label") or "", 180),
                "story_job": item.get("story_job"),
                "status": item.get("status"),
                "source_refs": item.get("source_refs") or [],
                "required_atom_kinds": item.get("required_atom_kinds") or [],
            }
            for item in jobs
            if str(item.get("job_id") or "") != current_job_id
        ],
        "global_atom_count": len(atoms),
        "open_gap_count": sum(1 for item in gaps if item.get("status") in {"open", "deferred"}),
        "risk_count": len(workspace.get("risk_register") or []),
        "selected_evidence_count": len(workspace.get("selected_evidence_ids") or []),
    }


def research_job_prompt_payload(
    workspace: dict[str, Any], job_id: str, *, source_map_summary: dict[str, Any]
) -> dict[str, Any]:
    workspace = prompt_workspace(workspace)
    job = workspace_job(workspace, job_id) or {}
    return {
        "job": current_job_prompt_payload(job),
        "workspace_progress": research_job_workspace_progress_for_prompt(
            workspace,
            current_job_id=job_id,
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
    "prompt_atom_index_payload",
    "prompt_gap_payload",
    "prompt_workspace",
    "research_finalizer_payload_for_prompt",
    "research_job_prompt_payload",
    "research_job_workspace_progress_for_prompt",
    "research_workspace_summary_for_prompt",
    "workspace_job",
]
