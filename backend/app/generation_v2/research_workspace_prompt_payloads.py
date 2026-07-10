from __future__ import annotations

from typing import Any

from app.generation_v2.research_source_map_prompting import source_map_summary_for_job
from app.generation_v2.research_workspace_contracts import (
    RESEARCH_READS_PER_COMMIT_ADVISORY,
    RESEARCH_READS_PER_COMMIT_WARNING,
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
        "question": str(gap.get("question") or "")[:600],
        "category": gap.get("category"),
        "severity": gap.get("severity"),
        "treatment_relevance": _treatment_relevance(gap.get("treatment_relevance")),
        "status": gap.get("status"),
        "source_refs": gap.get("source_refs") or [],
        "atom_refs": gap.get("atom_refs") or [],
        "evidence_count": len(evidence_ids),
        "has_evidence": bool(evidence_ids),
        "resolution_note": str(gap.get("resolution_note") or "")[:500],
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
        "claim": str(atom.get("claim") or "")[:700],
        "explanation": str(atom.get("explanation") or "")[:900],
        "story_value": str(atom.get("story_value") or "")[:500],
        "source_refs": atom.get("source_refs") or [],
        "evidence_count": len(evidence_ids),
        "has_evidence": bool(evidence_ids),
        "confidence": atom.get("confidence"),
        "risk": atom.get("risk"),
        "source_coverage_status": atom.get("source_coverage_status"),
        "story_weight": _story_weight(str(atom.get("story_weight") or ""), kind=str(atom.get("kind") or "")),
        "treatment_role_hint": str(atom.get("treatment_role_hint") or "")[:400],
    }
    if include_evidence_ids and evidence_ids:
        payload["evidence_ids"] = evidence_ids
    return payload


def research_workspace_summary_for_prompt(
    workspace: dict[str, Any],
    *,
    max_jobs: int = 30,
    max_atoms: int = 30,
    include_atom_evidence_ids: bool = True,
    include_gap_evidence_ids: bool = True,
) -> dict[str, Any]:
    workspace = prompt_workspace(workspace)
    return {
        "status": workspace.get("status"),
        "source_count": len(workspace.get("source_order") or []),
        "job_count": len(workspace.get("investigation_jobs") or []),
        "current_job": workspace.get("current_job"),
        "processed_jobs": workspace.get("processed_jobs") or [],
        "covered_source_refs": workspace.get("covered_source_refs") or [],
        "selected_evidence_count": len(workspace.get("selected_evidence_ids") or []),
        "research_gaps": [
            prompt_gap_payload(item, include_evidence_ids=include_gap_evidence_ids)
            for item in _dedupe_gaps(workspace.get("research_gaps") or [])[:80]
        ],
        "risk_count": len(workspace.get("risk_register") or []),
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
                "summary": item.get("summary"),
                "deferred_reason": item.get("deferred_reason"),
            }
            for item in (workspace.get("investigation_jobs") or [])[:max_jobs]
        ],
        "atom_drafts": [
            prompt_atom_payload(item, include_evidence_ids=include_atom_evidence_ids, atom_ref=f"A{idx:02d}")
            for idx, item in enumerate(_active_atom_drafts(workspace.get("atom_drafts") or [])[:max_atoms], start=1)
        ],
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
                    "note": str(observation.get("note") or "")[:700],
                    "pending_questions": [str(item)[:220] for item in observation.get("pending_questions") or []][:6],
                }
            )
    base = research_workspace_summary_for_prompt(
        workspace,
        max_jobs=max_jobs,
        max_atoms=max_atoms,
        include_atom_evidence_ids=False,
        include_gap_evidence_ids=False,
    )
    return {
        "status": base.get("status"),
        "source_count": base.get("source_count"),
        "job_count": base.get("job_count"),
        "selected_evidence_count": base.get("selected_evidence_count"),
        "jobs": base.get("jobs") or [],
        "atom_drafts": base.get("atom_drafts") or [],
        "observations": observations[:max_observations],
        "research_gaps": base.get("research_gaps") or [],
        "risk_register": [
            {
                "risk": str(item.get("risk") if isinstance(item, dict) else item)[:500],
                "severity": str(item.get("severity") or "medium")[:40] if isinstance(item, dict) else "medium",
                "source_refs": (item.get("source_refs") or []) if isinstance(item, dict) else [],
                "atom_refs": (item.get("atom_refs") or []) if isinstance(item, dict) else [],
                "evidence_count": len(item.get("evidence_ids") or []) if isinstance(item, dict) else 0,
                "has_evidence": bool(item.get("evidence_ids") or []) if isinstance(item, dict) else False,
            }
            for item in (workspace.get("risk_register") or [])[:40]
        ],
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
        "job": job,
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
    "prompt_atom_payload",
    "prompt_gap_payload",
    "prompt_workspace",
    "research_finalizer_payload_for_prompt",
    "research_job_prompt_payload",
    "research_workspace_summary_for_prompt",
    "workspace_job",
]
