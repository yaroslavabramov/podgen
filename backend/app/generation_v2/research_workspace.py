from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.research_kinds import ALLOWED_RESEARCH_ATOM_KINDS, ATOM_KIND_GUIDANCE
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import EditorialAtom, ResearchMemo, SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_SELECTED
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType, V2ToolTrace


WORKSPACE_ARTIFACT_KEY = "main"
WORKSPACE_SCHEMA_VERSION = "research_workspace.v2"
RESEARCH_READ_TOOLS = {
    "search_corpus",
    "find_quotes",
    "read_evidence",
    "inspect_source",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "skim_source",
    "compare_evidence_for_question",
}
RESEARCH_SEMANTIC_COMMIT_TOOLS = {
    "merge_editorial_atoms",
    "record_research_observation",
    "upsert_editorial_atom",
    "finish_research_job",
    "record_research_risk",
    "record_research_gap",
    "finalize_research_memo",
}
RESEARCH_READS_PER_COMMIT_WARNING = 6
RESEARCH_READS_PER_COMMIT_ADVISORY = 8
RESEARCH_GAP_CATEGORIES = {
    "missing_source",
    "weak_corroboration",
    "contradiction",
    "legal_detail",
    "chronology_detail",
    "optional_context",
}
RESEARCH_GAP_SEVERITIES = {"blocking", "non_blocking", "optional"}
RESEARCH_GAP_STATUSES = {"open", "resolved", "deferred"}
SOURCE_QUALITY_PATTERNS = (
    "source quality",
    "source-quality",
    "источник-качество",
    "weak support",
    "needs corroboration",
    "need corroboration",
    "missing corroboration",
    "secondary confirmation",
    "redundant secondary",
    "source coverage",
    "weakly sourced",
    "нужно добрать источник",
    "требует добора",
)


async def load_research_workspace(db: AsyncSession, generation_run_id: uuid.UUID) -> dict[str, Any] | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.RESEARCH_WORKSPACE,
            V2Artifact.artifact_key == WORKSPACE_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        return None
    return dict(artifact.payload or {})


async def load_research_workspace_artifact(db: AsyncSession, generation_run_id: uuid.UUID) -> V2Artifact | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.RESEARCH_WORKSPACE,
            V2Artifact.artifact_key == WORKSPACE_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def save_research_workspace(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    workspace: dict[str, Any],
    status: str = V2ArtifactStatus.DRAFT,
) -> V2Artifact:
    workspace = _normalize_workspace(workspace)
    workspace["updated_at"] = _now()
    return await save_artifact(
        db,
        generation_run_id=generation_run_id,
        artifact_type=V2ArtifactType.RESEARCH_WORKSPACE,
        artifact_key=WORKSPACE_ARTIFACT_KEY,
        payload=workspace,
        status=status,
    )


async def ensure_research_workspace(
    db: AsyncSession,
    *,
    run: GenerationRun,
    source_map: SourceMap,
    source_inventory: SourceMap,
) -> dict[str, Any]:
    existing = await load_research_workspace(db, run.id)
    source_order = _source_order(source_inventory)
    source_map_signature = _source_map_signature(source_map, source_inventory)
    if (
        existing
        and existing.get("schema_version") == WORKSPACE_SCHEMA_VERSION
        and existing.get("source_order") == source_order
        and existing.get("source_map_signature") == source_map_signature
    ):
        return _normalize_workspace(existing)
    workspace = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "source_map_signature": source_map_signature,
        "status": "planning",
        "source_order": source_order,
        "research_questions": [],
        "investigation_jobs": [],
        "current_job": None,
        "processed_jobs": [],
        "covered_source_refs": [],
        "selected_evidence_ids": [],
        "atom_drafts": [],
        "open_questions": [],
        "research_gaps": [],
        "risk_register": [],
        "memo_fields": {},
        "created_at": _now(),
        "updated_at": _now(),
    }
    await save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
    return workspace


async def enqueue_research_repair_jobs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    max_jobs: int = 4,
) -> list[str]:
    current = await load_research_workspace(db, run.id)
    if not current:
        return []
    current = _normalize_workspace(current)
    existing_job_ids = {str(item.get("job_id") or "") for item in current.get("investigation_jobs") or []}
    blocking_gaps = [
        gap
        for gap in _dedupe_gaps(current.get("research_gaps") or [])
        if gap.get("severity") == "blocking" and gap.get("status") in {"open", "deferred"}
    ]
    repair_jobs: list[dict[str, Any]] = []
    for gap in blocking_gaps[:max_jobs]:
        question = str(gap.get("question") or "").strip()
        if not question:
            continue
        gap_id = str(gap.get("gap_id") or _stable_id("", question, len(repair_jobs) + 1))
        job_id = _stable_id(f"research_repair_{gap_id}", question, len(existing_job_ids) + len(repair_jobs) + 1)
        if job_id in existing_job_ids:
            continue
        source_refs = _dedupe([str(item) for item in gap.get("source_refs") or [] if str(item).strip()])
        allow_broad_search = bool(gap.get("category") == "missing_source" or not source_refs)
        repair_jobs.append(
            {
                "job_id": job_id,
                "label": f"Repair: {question[:120]}",
                "story_job": "research_repair",
                "purpose": (
                    "Resolve this blocking research gap from the previous memo gate. "
                    "If resolved, persist the source-grounded finding as an atom or observation, then call "
                    "record_research_gap with the same gap_id, status='resolved', resolving evidence_ids, "
                    "and a resolution_note. If it cannot be resolved, update the same gap as open/deferred "
                    "with a concrete reason."
                ),
                "source_refs": source_refs,
                "questions": [question],
                "required_atom_kinds": _repair_atom_kinds_for_gap(gap),
                "priority": "repair",
                "allow_broad_search": allow_broad_search,
                "status": "planned",
                "observations": [],
                "atom_ids": [],
                "evidence_ids": [],
                "summary": "",
                "deferred_reason": "",
                "repair_for_gap_id": gap_id,
                "repair_gap": {
                    "gap_id": gap_id,
                    "question": question[:600],
                    "category": str(gap.get("category") or ""),
                    "severity": str(gap.get("severity") or "blocking"),
                    "source_refs": source_refs,
                    "evidence_ids": _dedupe([str(item) for item in gap.get("evidence_ids") or [] if str(item).strip()]),
                    "status": str(gap.get("status") or "open"),
                },
                "updated_at": _now(),
            }
        )
    if not repair_jobs:
        return []
    current["investigation_jobs"] = [*(current.get("investigation_jobs") or []), *repair_jobs]
    current["status"] = "repair_planned"
    current["current_job"] = None
    current["memo_fields"] = {}
    current["research_repair_cycle"] = int(current.get("research_repair_cycle") or 0) + 1
    await save_research_workspace(db, generation_run_id=run.id, workspace=current)
    return [str(job["job_id"]) for job in repair_jobs]


def build_research_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    source_inventory: SourceMap,
    evidence_toolset: Any | None = None,
    mode: str,
    current_job_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []

    async def upsert_research_job_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        source_order = set(str(item) for item in current.get("source_order") or [])
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {"accepted": False, "error": "source_refs must exist in this research workspace", "invalid_source_refs": invalid}
        allow_broad_search = bool(args.get("allow_broad_search"))
        if not source_refs and not allow_broad_search:
            return {"accepted": False, "error": "research job requires source_refs unless allow_broad_search=true"}
        label = str(args.get("label") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not label or not purpose:
            return {"accepted": False, "error": "label and purpose are required"}
        questions = [str(item)[:360] for item in args.get("questions") or [] if str(item).strip()][:10]
        if not questions:
            return {"accepted": False, "error": "at least one investigation question is required"}
        invalid_required_kinds = [
            str(item)
            for item in args.get("required_atom_kinds") or []
            if str(item).strip() and str(item).strip() not in ALLOWED_RESEARCH_ATOM_KINDS
        ]
        if invalid_required_kinds:
            return {
                "accepted": False,
                "error": "required_atom_kinds must use supported research atom kinds",
                "invalid_required_atom_kinds": invalid_required_kinds,
                "allowed_kinds": sorted(ALLOWED_RESEARCH_ATOM_KINDS),
            }
        job_id = _stable_id(str(args.get("job_id") or ""), label, len(current.get("investigation_jobs") or []) + 1)
        job = {
            "job_id": job_id,
            "label": label[:180],
            "story_job": str(args.get("story_job") or "general")[:80] or "general",
            "purpose": purpose[:900],
            "source_refs": source_refs,
            "questions": questions,
            "required_atom_kinds": [
                str(item)
                for item in args.get("required_atom_kinds") or []
                if str(item).strip() in ALLOWED_RESEARCH_ATOM_KINDS
            ][:8],
            "priority": str(args.get("priority") or "normal")[:50] or "normal",
            "allow_broad_search": allow_broad_search,
            "status": "planned",
            "observations": [],
            "atom_ids": [],
            "evidence_ids": [],
            "summary": "",
            "deferred_reason": "",
            "updated_at": _now(),
        }
        jobs = [item for item in current.get("investigation_jobs") or [] if item.get("job_id") != job_id]
        jobs.append(job)
        current["investigation_jobs"] = jobs
        current["status"] = "planning"
        current["research_questions"] = _dedupe([*(current.get("research_questions") or []), *questions])
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "planned_job_count": len(jobs), "source_count": len(source_refs)}

    async def finalize_research_plan_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        jobs = [dict(item) for item in current.get("investigation_jobs") or []]
        if not jobs:
            return {"accepted": False, "error": "Cannot finalize research plan without investigation jobs"}
        current["status"] = "planned"
        current["plan_summary"] = str(args.get("summary") or "")[:1800]
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "status": "planned", "job_count": len(jobs)}

    async def record_research_observation_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_workspace_refs = _invalid_workspace_refs(current, source_refs)
        if invalid_workspace_refs:
            return {
                "accepted": False,
                "error": "observation source_refs must exist in the research workspace",
                "invalid_source_refs": invalid_workspace_refs,
            }
        invalid_refs = _refs_outside_job(job, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "observation source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        note = str(args.get("note") or "").strip()
        if not note:
            return {"accepted": False, "error": "note is required"}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = await _invalid_job_evidence_ids(evidence_toolset, job, evidence_ids)
        if invalid_evidence:
            return {"accepted": False, "error": "observation evidence_ids must be observed evidence from this job scope", "invalid_evidence_ids": invalid_evidence}
        observation = {
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "note": note[:1800],
            "pending_questions": [str(item)[:360] for item in args.get("pending_questions") or [] if str(item).strip()][:8],
            "created_at": _now(),
        }
        job.setdefault("observations", []).append(observation)
        job["evidence_ids"] = _dedupe([*(job.get("evidence_ids") or []), *evidence_ids])
        job["updated_at"] = _now()
        _replace_workspace_job(current, job)
        _mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "observation_count": len(job.get("observations") or []), "evidence_count": len(evidence_ids)}

    async def upsert_editorial_atom_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace_job(current, job_id)
        if job is None:
            return {"accepted": False, "error": "Unknown research job", "job_id": job_id}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        if not source_refs:
            return {"accepted": False, "error": "upsert_editorial_atom requires source_refs"}
        invalid_workspace_refs = _invalid_workspace_refs(current, source_refs)
        if invalid_workspace_refs:
            return {
                "accepted": False,
                "error": "atom source_refs must exist in the research workspace",
                "invalid_source_refs": invalid_workspace_refs,
            }
        invalid_refs = _refs_outside_job(job, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "atom source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        if not evidence_ids:
            return {"accepted": False, "error": "upsert_editorial_atom requires at least one evidence_id"}
        if evidence_toolset is None:
            return {"accepted": False, "error": "upsert_editorial_atom requires evidence validation tools"}
        invalid_evidence = await _invalid_job_evidence_ids(evidence_toolset, job, evidence_ids, source_refs=source_refs)
        if invalid_evidence:
            return {"accepted": False, "error": "atom evidence_ids must belong to selected source_refs and current job scope", "invalid_evidence_ids": invalid_evidence}
        source_ids = await _source_ids_for_refs(evidence_toolset, source_inventory, source_refs)
        if not source_ids:
            return {"accepted": False, "error": "source_refs could not be resolved"}
        kind = _atom_kind(str(args.get("kind") or ""))
        if kind is None:
            return {
                "accepted": False,
                "error": "atom kind must be one of the supported research finding kinds",
                "allowed_kinds": sorted(ALLOWED_RESEARCH_ATOM_KINDS),
                "kind_guidance": ATOM_KIND_GUIDANCE,
            }
        title = str(args.get("title") or "").strip()
        claim = str(args.get("claim") or "").strip()
        story_value = str(args.get("story_value") or "").strip()
        if not title or not claim or not story_value:
            return {"accepted": False, "error": "title, claim, and story_value are required"}
        if _looks_like_source_quality_note(title, claim, story_value, str(args.get("explanation") or "")):
            return {
                "accepted": False,
                "error": "source-quality notes are not editorial atoms; use record_research_risk or record_research_gap",
            }
        for evidence_id in evidence_ids:
            await evidence_toolset._mark_evidence_status(
                evidence_id,
                EVIDENCE_STATUS_SELECTED,
                {
                    "selected_purpose": f"research_atom:{kind}",
                    "selected_role": "research_atom",
                    "selected_rationale": title[:240],
                },
            )
        atom_id = _stable_id(str(args.get("atom_id") or ""), title, len(current.get("atom_drafts") or []) + 1)
        draft = {
            "atom_id": atom_id,
            "job_id": job_id,
            "kind": kind,
            "title": title[:220],
            "claim": claim[:1400],
            "explanation": str(args.get("explanation") or claim)[:1800],
            "source_refs": source_refs,
            "source_ids": source_ids,
            "quote_refs": [str(item)[:200] for item in args.get("quote_refs") or [] if str(item).strip()][:8],
            "story_value": story_value[:900],
            "anchor_refs": [str(item)[:120] for item in args.get("anchor_refs") or [] if str(item).strip()][:8],
            "evidence_ids": evidence_ids,
            "confidence": _confidence(args.get("confidence")),
            "risk": str(args.get("risk") or "medium")[:40] or "medium",
            "source_coverage_status": _coverage_status(str(args.get("source_coverage_status") or "")),
            "created_at": _now(),
            "updated_at": _now(),
        }
        current["atom_drafts"] = [item for item in current.get("atom_drafts") or [] if item.get("atom_id") != atom_id]
        current["atom_drafts"].append(draft)
        job["atom_ids"] = _dedupe([*(job.get("atom_ids") or []), atom_id])
        job["evidence_ids"] = _dedupe([*(job.get("evidence_ids") or []), *evidence_ids])
        job["updated_at"] = _now()
        _replace_workspace_job(current, job)
        _mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "atom_id": atom_id, "job_id": job_id, "evidence_count": len(evidence_ids), "atom_count": len(current["atom_drafts"])}

    async def finish_research_job_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace_job(current, job_id)
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
        job["status"] = status
        job["summary"] = summary[:1600]
        job["deferred_reason"] = str(args.get("deferred_reason") or "")[:1000]
        job["updated_at"] = _now()
        _replace_workspace_job(current, job)
        processed = [item for item in current.get("processed_jobs") or [] if item.get("job_id") != job_id]
        processed.append({"job_id": job_id, "status": status, "summary": job["summary"], "updated_at": _now()})
        current["processed_jobs"] = processed
        current["current_job"] = None
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "status": status, "processed_job_count": len(processed)}

    async def record_research_risk_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace_job(current, job_id) if job_id else None
        risk = str(args.get("risk") or "").strip()
        if not risk:
            return {"accepted": False, "error": "risk is required"}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_refs = [ref for ref in source_refs if ref not in set(current.get("source_order") or [])]
        if invalid_refs:
            return {"accepted": False, "error": "risk source_refs must exist in workspace", "invalid_source_refs": invalid_refs}
        if job is not None:
            invalid_refs = _refs_outside_job(job, source_refs)
            if invalid_refs:
                return {"accepted": False, "error": "risk source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = (
            await _invalid_job_evidence_ids(evidence_toolset, job, evidence_ids, source_refs=source_refs)
            if job is not None
            else await _invalid_workspace_evidence_ids(evidence_toolset, evidence_ids)
        )
        if invalid_evidence:
            error = (
                "risk evidence_ids must belong to selected source_refs and current job scope"
                if job is not None
                else "risk evidence_ids must exist in this run"
            )
            return {"accepted": False, "error": error, "invalid_evidence_ids": invalid_evidence}
        current.setdefault("risk_register", []).append(
            {
                "risk": risk[:1200],
                "severity": str(args.get("severity") or "medium")[:40] or "medium",
                "source_refs": source_refs,
                "evidence_ids": evidence_ids,
                "created_at": _now(),
            }
        )
        _mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "risk_count": len(current.get("risk_register") or [])}

    async def record_research_gap_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        job_id = str(args.get("job_id") or current_job_id or (current.get("current_job") or {}).get("job_id") or "")
        job = _workspace_job(current, job_id) if job_id else None
        question = str(args.get("question") or "").strip()
        if not question:
            return {"accepted": False, "error": "gap question is required"}
        category = str(args.get("category") or "").strip()
        if category not in RESEARCH_GAP_CATEGORIES:
            return {"accepted": False, "error": "gap category is invalid", "allowed_categories": sorted(RESEARCH_GAP_CATEGORIES)}
        severity = str(args.get("severity") or "").strip()
        if severity not in RESEARCH_GAP_SEVERITIES:
            return {"accepted": False, "error": "gap severity is invalid", "allowed_severities": sorted(RESEARCH_GAP_SEVERITIES)}
        status = str(args.get("status") or "open").strip() or "open"
        if status not in RESEARCH_GAP_STATUSES:
            return {"accepted": False, "error": "gap status is invalid", "allowed_statuses": sorted(RESEARCH_GAP_STATUSES)}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_refs = _invalid_workspace_refs(current, source_refs)
        if invalid_refs:
            return {"accepted": False, "error": "gap source_refs must exist in workspace", "invalid_source_refs": invalid_refs}
        if job is not None:
            invalid_refs = _refs_outside_job(job, source_refs)
            if invalid_refs:
                return {"accepted": False, "error": "gap source_refs must belong to the current research job", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = (
            await _invalid_job_evidence_ids(evidence_toolset, job, evidence_ids, source_refs=source_refs)
            if job is not None
            else await _invalid_workspace_evidence_ids(evidence_toolset, evidence_ids)
        )
        if invalid_evidence:
            error = (
                "gap evidence_ids must belong to selected source_refs and current job scope"
                if job is not None
                else "gap evidence_ids must exist in this run"
            )
            return {"accepted": False, "error": error, "invalid_evidence_ids": invalid_evidence}
        resolution_note = str(args.get("resolution_note") or "").strip()
        if status == "resolved" and not resolution_note:
            return {"accepted": False, "error": "resolved research gaps require resolution_note"}
        if severity == "blocking" and status == "resolved" and not evidence_ids:
            return {"accepted": False, "error": "blocking research gaps cannot be resolved without evidence_ids"}
        if status == "resolved" and evidence_toolset is not None:
            for evidence_id in evidence_ids:
                await evidence_toolset._mark_evidence_status(
                    evidence_id,
                    EVIDENCE_STATUS_SELECTED,
                    {
                        "selected_purpose": "research_gap_resolution",
                        "selected_role": "research_gap_resolution",
                        "selected_rationale": resolution_note[:240],
                    },
                )
        normalized_key = _gap_key(category, question)
        gap_id = _stable_id(str(args.get("gap_id") or ""), question, len(current.get("research_gaps") or []) + 1)
        gap = {
            "gap_id": gap_id,
            "question": question[:600],
            "category": category,
            "severity": severity,
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "status": status,
            "resolution_note": resolution_note[:900],
            "normalized_key": normalized_key,
            "created_at": _now(),
            "updated_at": _now(),
        }
        existing = next((item for item in current.get("research_gaps") or [] if item.get("normalized_key") == normalized_key), None)
        gap = _merge_research_gap(existing, gap) if isinstance(existing, dict) else gap
        gaps = [item for item in current.get("research_gaps") or [] if item.get("normalized_key") != normalized_key]
        gaps.append(gap)
        current["research_gaps"] = gaps
        _mark_research_progress(current, source_refs=source_refs, evidence_ids=evidence_ids)
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {
            "accepted": True,
            "gap_id": gap.get("gap_id") or gap_id,
            "gap_count": len(gaps),
            "status": gap.get("status"),
            "evidence_count": len(gap.get("evidence_ids") or []),
        }

    async def merge_editorial_atoms_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        primary_atom_id = str(args.get("primary_atom_id") or "").strip()
        duplicate_atom_ids = _dedupe([str(item) for item in args.get("duplicate_atom_ids") or [] if str(item).strip()])
        if not primary_atom_id:
            return {"accepted": False, "error": "primary_atom_id is required"}
        if not duplicate_atom_ids:
            return {"accepted": False, "error": "duplicate_atom_ids is required"}
        if primary_atom_id in duplicate_atom_ids:
            return {"accepted": False, "error": "primary_atom_id cannot also be a duplicate_atom_id"}

        drafts = [dict(item) for item in current.get("atom_drafts") or [] if isinstance(item, dict)]
        by_id = {str(item.get("atom_id") or ""): item for item in drafts if str(item.get("atom_id") or "").strip()}
        primary = by_id.get(primary_atom_id)
        if primary is None:
            return {"accepted": False, "error": "primary_atom_id not found", "missing_atom_ids": [primary_atom_id]}
        missing = [atom_id for atom_id in duplicate_atom_ids if atom_id not in by_id]
        if missing:
            return {"accepted": False, "error": "duplicate_atom_id not found", "missing_atom_ids": missing}
        inactive = [
            atom_id
            for atom_id in [primary_atom_id, *duplicate_atom_ids]
            if str(by_id[atom_id].get("merged_into") or "").strip() or str(by_id[atom_id].get("status") or "") == "merged"
        ]
        if inactive:
            return {"accepted": False, "error": "cannot merge atom drafts that are already merged", "inactive_atom_ids": inactive}
        primary_kind = str(primary.get("kind") or "")
        cross_kind = [atom_id for atom_id in duplicate_atom_ids if str(by_id[atom_id].get("kind") or "") != primary_kind]
        if cross_kind:
            return {
                "accepted": False,
                "error": "merge_editorial_atoms only merges atom drafts with the same kind",
                "primary_kind": primary_kind,
                "cross_kind_atom_ids": cross_kind,
            }

        merged = _merge_atom_drafts_explicit(primary, [by_id[atom_id] for atom_id in duplicate_atom_ids], args)
        merged_at = _now()
        updated_drafts: list[dict[str, Any]] = []
        for draft in drafts:
            atom_id = str(draft.get("atom_id") or "")
            if atom_id == primary_atom_id:
                updated_drafts.append(merged)
            elif atom_id in duplicate_atom_ids:
                retired = dict(draft)
                retired["merged_into"] = primary_atom_id
                retired["merged_at"] = merged_at
                retired["status"] = "merged"
                updated_drafts.append(retired)
            else:
                updated_drafts.append(draft)
        current["atom_drafts"] = updated_drafts
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {
            "accepted": True,
            "primary_atom_id": primary_atom_id,
            "merged_duplicate_count": len(duplicate_atom_ids),
            "evidence_count": len(merged.get("evidence_ids") or []),
            "source_ref_count": len(merged.get("source_refs") or []),
        }

    async def finalize_research_memo_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        active_content_drafts = [
            draft
            for draft in _active_atom_drafts(current.get("atom_drafts") or [])
            if not _draft_is_source_quality_note(draft)
        ]
        if not active_content_drafts:
            return {"accepted": False, "error": "Cannot finalize research memo without active content atom drafts"}
        title = str(args.get("title") or "").strip()
        thesis = str(args.get("thesis") or "").strip()
        research_synthesis = str(args.get("research_synthesis") or "").strip()
        if not title or not thesis or not research_synthesis:
            return {"accepted": False, "error": "title, thesis, and research_synthesis are required"}
        current["memo_fields"] = {
            "title": title[:240],
            "thesis": thesis[:1800],
            "research_synthesis": research_synthesis[:1800],
            "source_coverage": [str(item)[:400] for item in args.get("source_coverage") or [] if str(item).strip()][:40],
            "risk_register": [str(item)[:500] for item in args.get("risk_register") or [] if str(item).strip()][:40],
        }
        current["status"] = "finalized"
        _reset_research_read_advisory(current)
        await save_research_workspace(db, generation_run_id=run.id, workspace=current, status=V2ArtifactStatus.PASSED)
        return {"accepted": True, "status": "finalized", "atom_count": len(_active_atom_drafts(current.get("atom_drafts") or []))}

    if mode == "planner":
        tools.extend(
            [
                AgentTool(
                    name="upsert_research_job",
                    description="Persist one investigation job. The planner uses this to split SourceMap questions into durable research work.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "label": {"type": "string"},
                            "story_job": {"type": "string"},
                            "purpose": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "questions": {"type": "array", "items": {"type": "string"}},
                            "required_atom_kinds": {"type": "array", "items": {"type": "string", "enum": sorted(ALLOWED_RESEARCH_ATOM_KINDS)}},
                            "priority": {"type": "string"},
                            "allow_broad_search": {"type": "boolean"},
                        },
                        "required": [
                            "label",
                            "story_job",
                            "purpose",
                            "source_refs",
                            "questions",
                            "required_atom_kinds",
                            "priority",
                            "allow_broad_search",
                        ],
                    },
                    handler=upsert_research_job_tool,
                ),
                AgentTool(
                    name="finalize_research_plan",
                    description="Mark the durable research plan complete after investigation jobs have been written.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                        },
                        "required": ["summary"],
                    },
                    handler=finalize_research_plan_tool,
                ),
            ]
        )
    if mode == "job":
        tools.extend(
            [
                AgentTool(
                    name="record_research_observation",
                    description="Persist an intermediate research finding for the current job before reading more or crossing a memory boundary.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "note": {"type": "string"},
                            "pending_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["job_id", "source_refs", "evidence_ids", "note", "pending_questions"],
                    },
                    handler=record_research_observation_tool,
                ),
                AgentTool(
                    name="upsert_editorial_atom",
                    description="Persist one source-grounded editorial atom draft. Included evidence_ids are selected automatically for research lineage.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "atom_id": {"type": "string"},
                            "kind": {"type": "string", "enum": sorted(ALLOWED_RESEARCH_ATOM_KINDS)},
                            "title": {"type": "string"},
                            "claim": {"type": "string"},
                            "explanation": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "quote_refs": {"type": "array", "items": {"type": "string"}},
                            "story_value": {"type": "string"},
                            "anchor_refs": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "risk": {"type": "string"},
                            "source_coverage_status": {"type": "string", "enum": ["ready", "partial", "weak"]},
                        },
                        "required": [
                            "job_id",
                            "kind",
                            "title",
                            "claim",
                            "explanation",
                            "source_refs",
                            "evidence_ids",
                            "quote_refs",
                            "story_value",
                            "anchor_refs",
                            "confidence",
                            "risk",
                            "source_coverage_status",
                        ],
                    },
                    handler=upsert_editorial_atom_tool,
                ),
                AgentTool(
                    name="finish_research_job",
                    description="Finish the current investigation job after persisting observations or atom drafts.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "status": {"type": "string", "enum": ["mapped", "deferred"]},
                            "summary": {"type": "string"},
                            "deferred_reason": {"type": "string"},
                        },
                        "required": ["job_id", "status", "summary", "deferred_reason"],
                    },
                    handler=finish_research_job_tool,
                ),
                AgentTool(
                    name="record_research_risk",
                    description="Persist a non-blocking source-quality, corroboration, contradiction, or uncertainty risk. Use this instead of an editorial atom for source-quality warnings.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "risk": {"type": "string"},
                            "severity": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["risk", "severity", "source_refs", "evidence_ids"],
                    },
                    handler=record_research_risk_tool,
                ),
                AgentTool(
                    name="record_research_gap",
                    description="Persist a structured research gap. Use severity=blocking only when treatment should not proceed without repair.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "gap_id": {"type": "string"},
                            "question": {"type": "string"},
                            "category": {"type": "string", "enum": sorted(RESEARCH_GAP_CATEGORIES)},
                            "severity": {"type": "string", "enum": sorted(RESEARCH_GAP_SEVERITIES)},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "status": {"type": "string", "enum": sorted(RESEARCH_GAP_STATUSES)},
                            "resolution_note": {"type": "string"},
                        },
                        "required": [
                            "question",
                            "category",
                            "severity",
                            "source_refs",
                            "evidence_ids",
                            "status",
                            "resolution_note",
                        ],
                    },
                    handler=record_research_gap_tool,
                ),
            ]
        )
    if mode == "finalizer":
        tools.extend(
            [
                AgentTool(
                    name="merge_editorial_atoms",
                    description=(
                        "Mandatory finalizer duplicate-audit tool. Merge same-kind atom drafts that repeat "
                        "the same research finding before final memo assembly."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "primary_atom_id": {"type": "string"},
                            "duplicate_atom_ids": {"type": "array", "items": {"type": "string"}},
                            "title": {"type": "string"},
                            "claim": {"type": "string"},
                            "explanation": {"type": "string"},
                            "story_value": {"type": "string"},
                            "risk": {"type": "string"},
                            "source_coverage_status": {"type": "string", "enum": ["ready", "partial", "weak"]},
                        },
                        "required": ["primary_atom_id", "duplicate_atom_ids"],
                    },
                    handler=merge_editorial_atoms_tool,
                ),
                AgentTool(
                    name="record_research_risk",
                    description="Persist a source-coverage, contradiction, or uncertainty risk that should survive memo finalization.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "risk": {"type": "string"},
                            "severity": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["risk", "severity", "source_refs", "evidence_ids"],
                    },
                    handler=record_research_risk_tool,
                ),
                AgentTool(
                    name="record_research_gap",
                    description="Persist or update a structured research gap before memo finalization. Blocking open/deferred gaps will block the research gate.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "gap_id": {"type": "string"},
                            "question": {"type": "string"},
                            "category": {"type": "string", "enum": sorted(RESEARCH_GAP_CATEGORIES)},
                            "severity": {"type": "string", "enum": sorted(RESEARCH_GAP_SEVERITIES)},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "status": {"type": "string", "enum": sorted(RESEARCH_GAP_STATUSES)},
                            "resolution_note": {"type": "string"},
                        },
                        "required": [
                            "question",
                            "category",
                            "severity",
                            "source_refs",
                            "evidence_ids",
                            "status",
                            "resolution_note",
                        ],
                    },
                    handler=record_research_gap_tool,
                ),
                AgentTool(
                    name="finalize_research_memo",
                    description=(
                        "Persist final memo fields. The backend assembles ResearchMemo atoms and structured risks "
                        "from durable workspace drafts; risk_register is optional extra cautions only."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "thesis": {"type": "string"},
                            "research_synthesis": {"type": "string"},
                            "source_coverage": {"type": "array", "items": {"type": "string"}},
                            "risk_register": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "title",
                            "thesis",
                            "research_synthesis",
                            "source_coverage",
                        ],
                    },
                    handler=finalize_research_memo_tool,
                ),
            ]
        )
    return tools


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
    job["started_at"] = _now()
    job["updated_at"] = _now()
    _replace_workspace_job(current, job)
    current["current_job"] = {"job_id": job_id, "started_at": job["started_at"], "source_refs": job.get("source_refs") or []}
    _reset_research_read_advisory(current)
    await save_research_workspace(db, generation_run_id=run.id, workspace=current)
    return current


async def research_workflow_guard_error(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_job_id: str | None,
) -> dict[str, Any] | None:
    """Update advisory read counters for research jobs without blocking reads."""
    await research_workflow_commit_advisory(
        db,
        generation_run_id=generation_run_id,
        current_job_id=current_job_id,
    )
    return None


async def research_workflow_commit_advisory(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_job_id: str | None,
) -> dict[str, Any] | None:
    if not current_job_id:
        return None
    workspace = await load_research_workspace(db, generation_run_id)
    current_job = (workspace or {}).get("current_job") or {}
    started_at = _parse_dt(current_job.get("started_at"))
    if started_at is None:
        return None
    commit_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_editorial_research",
            V2ToolTrace.created_at >= started_at,
            V2ToolTrace.tool_name.in_(list(RESEARCH_SEMANTIC_COMMIT_TOOLS)),
        )
        .order_by(V2ToolTrace.created_at.desc())
        .limit(1)
    )
    last_commit = commit_result.scalar_one_or_none()
    since = last_commit.created_at if last_commit is not None else started_at
    read_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_editorial_research",
            V2ToolTrace.created_at >= since,
            V2ToolTrace.tool_name.in_(list(RESEARCH_READ_TOOLS)),
        )
    )
    read_count = len(list(read_result.scalars()))
    advisory = {
        "reads_since_commit": read_count,
        "warning_threshold": RESEARCH_READS_PER_COMMIT_WARNING,
        "advisory_threshold": RESEARCH_READS_PER_COMMIT_ADVISORY,
        "commit_recommended": read_count >= RESEARCH_READS_PER_COMMIT_WARNING,
        "suggested_next_actions": [
            "record_research_observation",
            "upsert_editorial_atom",
            "record_research_risk",
            "record_research_gap",
        ],
    }
    if workspace is not None and (
        workspace.get("reads_since_commit") != read_count
        or workspace.get("commit_recommended") != advisory["commit_recommended"]
    ):
        workspace["reads_since_commit"] = read_count
        workspace["commit_recommended"] = advisory["commit_recommended"]
        await save_research_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return advisory if advisory["commit_recommended"] else None


def research_workspace_summary_for_prompt(workspace: dict[str, Any], *, max_jobs: int = 30, max_atoms: int = 30) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    return {
        "status": workspace.get("status"),
        "source_count": len(workspace.get("source_order") or []),
        "job_count": len(workspace.get("investigation_jobs") or []),
        "current_job": workspace.get("current_job"),
        "processed_jobs": workspace.get("processed_jobs") or [],
        "covered_source_refs": workspace.get("covered_source_refs") or [],
        "selected_evidence_count": len(workspace.get("selected_evidence_ids") or []),
        "research_gaps": _dedupe_gaps(workspace.get("research_gaps") or [])[:80],
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
            {
                "atom_id": item.get("atom_id"),
                "job_id": item.get("job_id"),
                "kind": item.get("kind"),
                "title": item.get("title"),
                "claim": str(item.get("claim") or "")[:700],
                "explanation": str(item.get("explanation") or "")[:900],
                "story_value": str(item.get("story_value") or "")[:500],
                "source_refs": item.get("source_refs") or [],
                "evidence_ids": item.get("evidence_ids") or [],
                "confidence": item.get("confidence"),
                "risk": item.get("risk"),
                "source_coverage_status": item.get("source_coverage_status"),
            }
            for item in _active_atom_drafts(workspace.get("atom_drafts") or [])[:max_atoms]
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


def research_finalizer_payload_for_prompt(workspace: dict[str, Any], *, max_jobs: int = 60, max_atoms: int = 60, max_observations: int = 120) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    observations: list[dict[str, Any]] = []
    for job in workspace.get("investigation_jobs") or []:
        for observation in job.get("observations") or []:
            observations.append(
                {
                    "job_id": job.get("job_id"),
                    "job_label": job.get("label"),
                    "source_refs": observation.get("source_refs") or [],
                    "evidence_ids": observation.get("evidence_ids") or [],
                    "note": str(observation.get("note") or "")[:700],
                    "pending_questions": [str(item)[:220] for item in observation.get("pending_questions") or []][:6],
                }
            )
    base = research_workspace_summary_for_prompt(workspace, max_jobs=max_jobs, max_atoms=max_atoms)
    return {
        "status": base.get("status"),
        "source_count": base.get("source_count"),
        "job_count": base.get("job_count"),
        "selected_evidence_count": base.get("selected_evidence_count"),
        "jobs": base.get("jobs") or [],
        "atom_drafts": base.get("atom_drafts") or [],
        "observations": observations[:max_observations],
        "research_gaps": _dedupe_gaps(workspace.get("research_gaps") or [])[:80],
        "risk_register": [
            {
                "risk": str(item.get("risk") if isinstance(item, dict) else item)[:500],
                "severity": str(item.get("severity") or "medium")[:40] if isinstance(item, dict) else "medium",
                "source_refs": (item.get("source_refs") or []) if isinstance(item, dict) else [],
                "evidence_ids": (item.get("evidence_ids") or []) if isinstance(item, dict) else [],
            }
            for item in (workspace.get("risk_register") or [])[:40]
        ],
        "assembly_instruction": (
            "Use atom_drafts as the memo backbone, but treat them as raw outputs from independent jobs that "
            "may overlap. Audit same-kind atom_drafts for repeated findings before finalization and use "
            "merge_editorial_atoms for duplicates. Use observations and research_gaps to explain coverage "
            "and risks. Do not invent atoms that were not persisted as atom_drafts. Blocking open/deferred "
            "gaps become final open_questions; non-blocking/optional gaps become risk/source-coverage notes."
        ),
    }


def research_job_prompt_payload(workspace: dict[str, Any], job_id: str, *, source_map_summary: dict[str, Any]) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    job = _workspace_job(workspace, job_id) or {}
    return {
        "job": job,
        "workspace_progress": research_workspace_summary_for_prompt(workspace, max_jobs=40, max_atoms=40),
        "source_map_summary": _source_map_summary_for_job(source_map_summary, job),
        "commit_advisory": {
            "reads_since_commit": workspace.get("reads_since_commit") or 0,
            "warning_threshold": RESEARCH_READS_PER_COMMIT_WARNING,
            "advisory_threshold": RESEARCH_READS_PER_COMMIT_ADVISORY,
            "commit_recommended": bool(workspace.get("commit_recommended")),
        },
    }


def unprocessed_research_jobs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("investigation_jobs") or []
        if item.get("status") == "planned"
    ]


def unresolved_research_jobs(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("investigation_jobs") or []
        if item.get("status") in {"planned", "in_progress"}
    ]


def research_memo_from_workspace(run: GenerationRun, workspace: dict[str, Any]) -> ResearchMemo:
    workspace = _normalize_workspace(workspace)
    fields = dict(workspace.get("memo_fields") or {})
    title = fields.get("title") or str((run.config or {}).get("user_prompt") or "Podcast episode")[:120]
    atoms: list[EditorialAtom] = []
    source_quality_risks: list[str] = []
    for draft in _active_atom_drafts(workspace.get("atom_drafts") or []):
        evidence_ids = _dedupe([str(item) for item in draft.get("evidence_ids") or [] if str(item).strip()])
        anchor_refs = _dedupe([*(draft.get("anchor_refs") or []), *evidence_ids])
        if _draft_is_source_quality_note(draft):
            source_quality_risks.append(_source_quality_risk_from_draft(draft))
            continue
        atoms.append(
            EditorialAtom(
                kind=_coerce_atom_kind(str(draft.get("kind") or "")),  # type: ignore[arg-type]
                title=str(draft.get("title") or "Editorial atom")[:220],
                explanation=str(draft.get("explanation") or draft.get("claim") or "")[:1800],
                source_ids=_dedupe([str(item) for item in draft.get("source_ids") or [] if str(item).strip()]),
                quote_refs=[str(item) for item in draft.get("quote_refs") or [] if str(item).strip()][:8],
                story_value=str(draft.get("story_value") or "Use in treatment.")[:900],
                anchor_refs=anchor_refs[:12],
                evidence_ids=evidence_ids,
                confidence=_confidence(draft.get("confidence")),
                risk=str(draft.get("risk") or "medium")[:600] or "medium",
                source_coverage_status=_coverage_status(str(draft.get("source_coverage_status") or "")),  # type: ignore[arg-type]
            )
        )
    if not atoms:
        raise RuntimeError("Research workspace has no active content atom drafts.")
    processed_summaries = [str(item.get("summary")) for item in workspace.get("processed_jobs") or [] if item.get("summary")]
    risks = [
        str(item.get("risk") if isinstance(item, dict) else item)
        for item in workspace.get("risk_register") or []
        if str(item.get("risk") if isinstance(item, dict) else item).strip()
    ]
    gaps = _dedupe_gaps(workspace.get("research_gaps") or [])
    blocking_open_questions = [
        str(item.get("question") or "")[:400]
        for item in gaps
        if item.get("severity") == "blocking" and item.get("status") in {"open", "deferred"} and str(item.get("question") or "").strip()
    ]
    nonblocking_gap_risks = [
        _risk_from_gap(item)
        for item in gaps
        if item.get("severity") in {"non_blocking", "optional"} and item.get("status") in {"open", "deferred"}
    ]
    structured_risks = [*risks, *nonblocking_gap_risks, *source_quality_risks]
    memo_field_risks = [str(item) for item in fields.get("risk_register") or [] if str(item).strip()]
    # Finalizer free-form risks are a compatibility fallback. In the normal workflow,
    # durable record_research_risk/record_research_gap entries are the source of truth.
    if structured_risks:
        memo_field_risks = []

    return ResearchMemo(
        title=title,
        thesis=fields.get("thesis") or f"The episode should investigate {title} through source-grounded mechanisms, stakes, and turns.",
        research_synthesis=(
            fields.get("research_synthesis")
            or fields.get("narrative_spine")
            or "The memo identifies source-grounded findings, mechanisms, chronology, disputes, consequences, and unresolved research risks."
        ),
        editorial_atoms=atoms,
        source_coverage=(fields.get("source_coverage") or processed_summaries)[:40],
        open_questions=_dedupe_normalized(blocking_open_questions)[:40],
        risk_register=_dedupe_normalized([*structured_risks, *memo_field_risks])[:40],
    )


def source_map_research_summary(
    source_map: SourceMap,
    source_inventory: SourceMap | None = None,
    *,
    include_evidence_ids: bool = True,
) -> dict[str, Any]:
    context = build_source_ref_context(source_inventory or source_map)
    def refs(source_ids: list[str]) -> list[str]:
        return [source_ref_for_id(str(source_id), context) for source_id in source_ids if source_id]

    source_cards = [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            "title": card.title,
            "domain": card.domain,
            "useful_for": card.useful_for[:8],
            "summary": card.summary[:700],
            "cautions": card.gaps_or_cautions[:6],
            **({"evidence_ids": card.evidence_ids[:8]} if include_evidence_ids and card.evidence_ids else {}),
        }
        for card in source_map.source_cards[:80]
    ]
    quote_cards = [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            **({"evidence_id": card.evidence_id} if include_evidence_ids and card.evidence_id else {}),
            "locator": card.locator,
            "narrative_use": card.narrative_use,
            "quote_excerpt": card.quote[:500],
        }
        for card in source_map.quote_cards[:60]
    ]
    clusters = [
        {
            "cluster_id": cluster.cluster_id,
            "label": cluster.label,
            "source_refs": refs(cluster.source_ids),
            "primary_source_refs": refs(cluster.primary_source_ids),
            "secondary_source_refs": refs(cluster.secondary_source_ids),
            "duplicate_source_refs": refs(cluster.duplicate_source_ids),
            **({"evidence_ids": cluster.evidence_ids[:10]} if include_evidence_ids and cluster.evidence_ids else {}),
            "rationale": cluster.rationale[:500],
        }
        for cluster in source_map.source_triage.source_clusters[:40]
    ]
    relationships = [
        {
            "source_ref": source_ref_for_id(relationship.source_id, context),
            "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
            "relationship": relationship.relationship,
            **({"evidence_ids": relationship.evidence_ids[:10]} if include_evidence_ids and relationship.evidence_ids else {}),
            "rationale": relationship.rationale[:400],
        }
        for relationship in source_map.source_triage.source_relationships[:60]
    ]
    gaps = _dedupe([*source_map.gaps[:20], *source_map.source_triage.gaps[:20]])
    return {
        "source_card_count": len(source_map.source_cards),
        "quote_card_count": len(source_map.quote_cards),
        "gap_count": len(gaps),
        "includes_evidence_ids": include_evidence_ids,
        "source_cards": source_cards,
        "quote_cards": quote_cards,
        "clusters": clusters,
        "relationships": relationships,
        "gaps": gaps,
    }


def _source_map_summary_for_job(source_map_summary: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    refs = set(str(item) for item in job.get("source_refs") or [] if str(item).strip())
    base = {
        "source_card_count": source_map_summary.get("source_card_count", 0),
        "quote_card_count": source_map_summary.get("quote_card_count", 0),
        "gap_count": source_map_summary.get("gap_count", 0),
        "cluster_count": len(source_map_summary.get("clusters") or []),
        "relationship_count": len(source_map_summary.get("relationships") or []),
        "includes_evidence_ids": bool(source_map_summary.get("includes_evidence_ids")),
        "filtered_for_source_refs": sorted(refs),
        "allow_broad_search": bool(job.get("allow_broad_search")),
    }
    if not refs:
        return {
            **base,
            "source_cards": [],
            "quote_cards": [],
            "clusters": [_compact_cluster_for_prompt(item) for item in (source_map_summary.get("clusters") or [])[:16]],
            "relationships": [],
            "gaps": [str(item)[:360] for item in (source_map_summary.get("gaps") or [])[:10]],
        }

    def has_ref(item: dict[str, Any], keys: list[str]) -> bool:
        item_refs: set[str] = set()
        for key in keys:
            value = item.get(key)
            if isinstance(value, list):
                item_refs.update(str(ref) for ref in value if str(ref).strip())
            elif value:
                item_refs.add(str(value))
        return bool(item_refs & refs)

    return {
        **base,
        "source_cards": [
            item
            for item in source_map_summary.get("source_cards") or []
            if str(item.get("source_ref") or "") in refs
        ],
        "quote_cards": [
            item
            for item in source_map_summary.get("quote_cards") or []
            if str(item.get("source_ref") or "") in refs
        ],
        "clusters": [
            item
            for item in source_map_summary.get("clusters") or []
            if has_ref(item, ["source_refs", "primary_source_refs", "secondary_source_refs", "duplicate_source_refs"])
        ],
        "relationships": [
            item
            for item in source_map_summary.get("relationships") or []
            if has_ref(item, ["source_ref", "related_source_ref"])
        ],
        "gaps": [str(item)[:360] for item in (source_map_summary.get("gaps") or [])[:10]],
    }


def _compact_cluster_for_prompt(cluster: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster_id": cluster.get("cluster_id"),
        "label": cluster.get("label"),
        "source_refs": (cluster.get("source_refs") or [])[:10],
        "primary_source_refs": (cluster.get("primary_source_refs") or [])[:8],
        "duplicate_source_refs": (cluster.get("duplicate_source_refs") or [])[:8],
    }


def _normalize_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(workspace or {})
    workspace.setdefault("schema_version", WORKSPACE_SCHEMA_VERSION)
    workspace.setdefault("source_order", [])
    workspace.setdefault("research_questions", [])
    workspace.setdefault("investigation_jobs", [])
    workspace.setdefault("processed_jobs", [])
    workspace.setdefault("covered_source_refs", [])
    workspace.setdefault("selected_evidence_ids", [])
    workspace.setdefault("atom_drafts", [])
    workspace.setdefault("open_questions", [])
    workspace.setdefault("research_gaps", [])
    workspace.setdefault("risk_register", [])
    workspace.setdefault("memo_fields", {})
    return workspace


def _workspace_job(workspace: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    for item in workspace.get("investigation_jobs") or []:
        if item.get("job_id") == job_id:
            return dict(item)
    return None


def _replace_workspace_job(workspace: dict[str, Any], job: dict[str, Any]) -> None:
    job_id = job.get("job_id")
    workspace["investigation_jobs"] = [job if item.get("job_id") == job_id else item for item in workspace.get("investigation_jobs") or []]


def _refs_outside_job(job: dict[str, Any], source_refs: list[str]) -> list[str]:
    if bool(job.get("allow_broad_search")):
        return []
    allowed = set(str(item) for item in job.get("source_refs") or [])
    return [ref for ref in source_refs if ref not in allowed]


def _invalid_workspace_refs(workspace: dict[str, Any], source_refs: list[str]) -> list[str]:
    source_order = set(str(item) for item in workspace.get("source_order") or [])
    return [ref for ref in source_refs if ref not in source_order]


async def _invalid_job_evidence_ids(
    evidence_toolset: Any | None,
    job: dict[str, Any],
    evidence_ids: list[str],
    *,
    source_refs: list[str] | None = None,
) -> list[str]:
    if not evidence_ids or evidence_toolset is None:
        return []
    allowed_refs = set(str(item) for item in (source_refs or job.get("source_refs") or []))
    invalid: list[str] = []
    for evidence_id in evidence_ids:
        evidence = await evidence_toolset._evidence_in_run(evidence_id)
        if evidence is None:
            invalid.append(evidence_id)
            continue
        evidence_ref = await evidence_toolset._source_ref_for_id(evidence.source_id)
        if not bool(job.get("allow_broad_search")) and evidence_ref not in set(job.get("source_refs") or []):
            invalid.append(evidence_id)
            continue
        if allowed_refs and evidence_ref not in allowed_refs:
            invalid.append(evidence_id)
    return invalid


async def _invalid_workspace_evidence_ids(evidence_toolset: Any | None, evidence_ids: list[str]) -> list[str]:
    if not evidence_ids or evidence_toolset is None:
        return []
    invalid = []
    for evidence_id in evidence_ids:
        if await evidence_toolset._evidence_in_run(evidence_id) is None:
            invalid.append(evidence_id)
    return invalid


async def _source_ids_for_refs(evidence_toolset: Any | None, inventory: SourceMap, refs: list[str]) -> list[str]:
    if evidence_toolset is not None:
        source_ids, invalid = await evidence_toolset._source_ids_from_refs_or_ids(refs)
        if not invalid:
            return source_ids
    context = build_source_ref_context(inventory)
    by_ref = {source_ref_for_id(card.source_id, context): card.source_id for card in inventory.source_cards}
    return _dedupe([by_ref.get(ref, "") for ref in refs])


def _mark_research_progress(workspace: dict[str, Any], *, source_refs: list[str], evidence_ids: list[str]) -> None:
    workspace["covered_source_refs"] = _dedupe([*(workspace.get("covered_source_refs") or []), *source_refs])
    workspace["selected_evidence_ids"] = _dedupe([*(workspace.get("selected_evidence_ids") or []), *evidence_ids])


def _reset_research_read_advisory(workspace: dict[str, Any]) -> None:
    workspace["reads_since_commit"] = 0
    workspace["commit_recommended"] = False


def _source_order(inventory: SourceMap) -> list[str]:
    context = build_source_ref_context(inventory)
    return [source_ref_for_id(card.source_id, context) for card in inventory.source_cards]


def _source_map_signature(source_map: SourceMap, source_inventory: SourceMap) -> dict[str, Any]:
    source_map_payload = source_map.model_dump(mode="json")
    inventory_payload = [
        {
            "source_id": card.source_id,
            "source_version_ids": card.source_version_ids,
            "title": card.title,
            "domain": card.domain,
            "source_type": card.source_type,
            "summary": card.summary,
            "useful_for": card.useful_for,
            "gaps_or_cautions": card.gaps_or_cautions,
            "coverage_status": card.coverage_status,
            "evidence_ids": card.evidence_ids,
        }
        for card in source_inventory.source_cards
    ]
    return {
        "schema_version": "research_source_map_signature.v2",
        "source_map_hash": _stable_hash(source_map_payload),
        "source_inventory_hash": _stable_hash(inventory_payload),
        "inventory_source_count": len(source_inventory.source_cards),
        "source_map_source_count": len(source_map.source_cards),
        "quote_count": len(source_map.quote_cards),
        "cluster_count": len(source_map.source_triage.source_clusters),
        "relationship_count": len(source_map.source_triage.source_relationships),
        "evidence_count": len(source_map.evidence_ids),
    }


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_id(requested: str, label: str, index: int) -> str:
    if requested:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in requested)[:56].strip("_") or f"item_{index}"
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())[:48].strip("_")
    return slug or f"item_{index}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _dedupe_normalized(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = _normalized_question_key(text)
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _dedupe_gaps(values: list[Any]) -> list[dict[str, Any]]:
    order: list[str] = []
    merged_by_key: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not question or category not in RESEARCH_GAP_CATEGORIES:
            continue
        gap = dict(raw)
        gap["category"] = category
        gap["severity"] = str(gap.get("severity") or "non_blocking") if str(gap.get("severity") or "") in RESEARCH_GAP_SEVERITIES else "non_blocking"
        gap["status"] = str(gap.get("status") or "open") if str(gap.get("status") or "") in RESEARCH_GAP_STATUSES else "open"
        gap["normalized_key"] = str(gap.get("normalized_key") or _gap_key(category, question))
        key = gap["normalized_key"]
        if key not in merged_by_key:
            order.append(key)
            merged_by_key[key] = gap
            continue
        merged_by_key[key] = _merge_research_gap(merged_by_key[key], gap)
    return [merged_by_key[key] for key in order]


def _active_atom_drafts(values: list[Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in values
        if isinstance(item, dict) and not str(item.get("merged_into") or "").strip() and str(item.get("status") or "") != "merged"
    ]


def _active_job_atom_count(workspace: dict[str, Any], job: dict[str, Any]) -> int:
    job_atom_ids = {str(item) for item in job.get("atom_ids") or [] if str(item).strip()}
    if not job_atom_ids:
        return 0
    return len(
        [
            draft
            for draft in _active_atom_drafts(workspace.get("atom_drafts") or [])
            if str(draft.get("atom_id") or "") in job_atom_ids
        ]
    )


def _merge_atom_drafts_explicit(primary: dict[str, Any], duplicates: list[dict[str, Any]], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    all_drafts = [primary, *duplicates]
    for key in ("source_refs", "source_ids", "quote_refs", "anchor_refs", "evidence_ids"):
        merged[key] = _dedupe([str(value) for draft in all_drafts for value in (draft.get(key) or []) if str(value).strip()])
    for key, limit in (("title", 220), ("claim", 1400), ("explanation", 1800), ("story_value", 900), ("risk", 40)):
        value = str(overrides.get(key) or "").strip()
        if value:
            merged[key] = value[:limit]
    if str(overrides.get("source_coverage_status") or "").strip():
        merged["source_coverage_status"] = _coverage_status(str(overrides.get("source_coverage_status") or ""))
    try:
        merged["confidence"] = max(float(draft.get("confidence") or 0.0) for draft in all_drafts)
    except (TypeError, ValueError):
        pass
    merged["updated_at"] = _now()
    merged["merged_atom_ids"] = _dedupe(
        [
            str(draft.get("atom_id") or "")
            for draft in all_drafts
            if str(draft.get("atom_id") or "").strip()
        ]
        + [
            str(atom_id)
            for draft in all_drafts
            for atom_id in (draft.get("merged_atom_ids") or [])
            if str(atom_id).strip()
        ]
    )
    return merged


def _merge_research_gap(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return incoming
    merged = dict(existing)
    incoming_status = str(incoming.get("status") or "open")
    incoming_resolution = str(incoming.get("resolution_note") or "").strip()
    severity_rank = {"optional": 0, "non_blocking": 1, "blocking": 2}
    existing_severity = str(existing.get("severity") or "non_blocking")
    incoming_severity = str(incoming.get("severity") or "non_blocking")
    strongest_severity = existing_severity if severity_rank.get(existing_severity, 1) > severity_rank.get(incoming_severity, 1) else incoming_severity
    existing_question = str(existing.get("question") or "").strip()
    if incoming_status == "resolved" and incoming_resolution:
        merged.update(incoming)
        merged["gap_id"] = existing.get("gap_id") or incoming.get("gap_id")
        merged["created_at"] = existing.get("created_at") or incoming.get("created_at")
        merged["severity"] = strongest_severity
        if existing_question:
            merged["question"] = existing_question
        return merged

    merged.update(incoming)
    merged["gap_id"] = existing.get("gap_id") or incoming.get("gap_id")
    merged["created_at"] = existing.get("created_at") or incoming.get("created_at")
    merged["severity"] = strongest_severity
    if existing_question:
        merged["question"] = existing_question
    if existing.get("status") == "resolved" and incoming_status != "resolved":
        merged["status"] = "resolved"
        merged["resolution_note"] = existing.get("resolution_note") or incoming.get("resolution_note") or ""
    return merged


def _gap_key(category: str, question: str) -> str:
    return f"{category}:{_normalized_question_key(question)}"


def _normalized_question_key(value: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\b(19|20)(\d{2})\s*-\s*(\d{2})\b", r"\1\2-\1\3", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _looks_like_source_quality_note(*parts: str) -> bool:
    text = " ".join(str(part or "").lower() for part in parts)
    return any(pattern in text for pattern in SOURCE_QUALITY_PATTERNS)


def _draft_is_source_quality_note(draft: dict[str, Any]) -> bool:
    return _looks_like_source_quality_note(
        str(draft.get("title") or ""),
        str(draft.get("claim") or ""),
        str(draft.get("explanation") or ""),
        str(draft.get("story_value") or ""),
        str(draft.get("risk") or ""),
    )


def _source_quality_risk_from_draft(draft: dict[str, Any]) -> str:
    title = str(draft.get("title") or "Source-quality note").strip()
    risk = str(draft.get("risk") or "").strip()
    return f"Source-quality note excluded from editorial atoms: {title}. {risk}".strip()


def _risk_from_gap(gap: dict[str, Any]) -> str:
    severity = str(gap.get("severity") or "non_blocking")
    category = str(gap.get("category") or "optional_context")
    question = str(gap.get("question") or "").strip()
    note = str(gap.get("resolution_note") or "").strip()
    suffix = f" {note}" if note else ""
    return f"{severity} research gap ({category}): {question}.{suffix}".strip()


def _repair_atom_kinds_for_gap(gap: dict[str, Any]) -> list[str]:
    category = str(gap.get("category") or "")
    if category == "legal_detail":
        return ["mechanism"]
    if category == "chronology_detail":
        return ["history", "turning_point"]
    if category == "contradiction":
        return ["conflict"]
    if category == "weak_corroboration":
        return ["consequence", "conflict"]
    return ["unanswered_question"]


def _atom_kind(value: str) -> str | None:
    text = str(value or "").strip()
    return text if text in ALLOWED_RESEARCH_ATOM_KINDS else None


def _coerce_atom_kind(value: str) -> str:
    return _atom_kind(value) or "unanswered_question"


def _coverage_status(value: str) -> str:
    text = str(value or "").strip()
    return text if text in {"ready", "partial", "weak"} else "partial"


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _now() -> str:
    return datetime.utcnow().isoformat()


__all__ = [
    "RESEARCH_READ_TOOLS",
    "RESEARCH_SEMANTIC_COMMIT_TOOLS",
    "build_research_workspace_tools",
    "enqueue_research_repair_jobs",
    "ensure_research_workspace",
    "load_research_workspace",
    "load_research_workspace_artifact",
    "research_finalizer_payload_for_prompt",
    "research_job_prompt_payload",
    "research_memo_from_workspace",
    "research_workspace_summary_for_prompt",
    "research_workflow_commit_advisory",
    "research_workflow_guard_error",
    "save_research_workspace",
    "source_map_research_summary",
    "start_research_job",
    "unprocessed_research_jobs",
    "unresolved_research_jobs",
]
