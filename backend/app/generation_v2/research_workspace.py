from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import EditorialAtom, ResearchMemo, SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_SELECTED
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType, V2ToolTrace


WORKSPACE_ARTIFACT_KEY = "main"
WORKSPACE_SCHEMA_VERSION = "research_workspace.v1"
RESEARCH_READ_TOOLS = {
    "search_corpus",
    "find_quotes",
    "read_evidence",
    "read_source_window",
    "read_source_with_model",
    "search_within_source",
    "compare_evidence_for_question",
}
RESEARCH_SEMANTIC_COMMIT_TOOLS = {
    "record_research_observation",
    "upsert_editorial_atom",
    "finish_research_job",
    "record_research_risk",
    "finalize_research_memo",
}
RESEARCH_READS_PER_COMMIT = 12


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
        "risk_register": [],
        "memo_fields": {},
        "created_at": _now(),
        "updated_at": _now(),
    }
    await save_research_workspace(db, generation_run_id=run.id, workspace=workspace)
    return workspace


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
        job_id = _stable_id(str(args.get("job_id") or ""), label, len(current.get("investigation_jobs") or []) + 1)
        job = {
            "job_id": job_id,
            "label": label[:180],
            "story_job": str(args.get("story_job") or "general")[:80] or "general",
            "purpose": purpose[:900],
            "source_refs": source_refs,
            "questions": questions,
            "required_atom_kinds": [str(item)[:80] for item in args.get("required_atom_kinds") or [] if str(item).strip()][:8],
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
        current["open_questions"] = _dedupe(
            [*(current.get("open_questions") or []), *[str(item)[:360] for item in args.get("open_questions") or [] if str(item).strip()]]
        )
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
        title = str(args.get("title") or "").strip()
        claim = str(args.get("claim") or "").strip()
        story_value = str(args.get("story_value") or "").strip()
        if not title or not claim or not story_value:
            return {"accepted": False, "error": "title, claim, and story_value are required"}
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
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "job_id": job_id, "status": status, "processed_job_count": len(processed)}

    async def record_research_risk_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        risk = str(args.get("risk") or "").strip()
        if not risk:
            return {"accepted": False, "error": "risk is required"}
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid_refs = [ref for ref in source_refs if ref not in set(current.get("source_order") or [])]
        if invalid_refs:
            return {"accepted": False, "error": "risk source_refs must exist in workspace", "invalid_source_refs": invalid_refs}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        invalid_evidence = await _invalid_workspace_evidence_ids(evidence_toolset, evidence_ids)
        if invalid_evidence:
            return {"accepted": False, "error": "risk evidence_ids must exist in this run", "invalid_evidence_ids": invalid_evidence}
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
        await save_research_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "risk_count": len(current.get("risk_register") or [])}

    async def finalize_research_memo_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_research_workspace(db, run.id) or workspace
        if not current.get("atom_drafts"):
            return {"accepted": False, "error": "Cannot finalize research memo without atom drafts"}
        title = str(args.get("title") or "").strip()
        thesis = str(args.get("thesis") or "").strip()
        narrative_spine = str(args.get("narrative_spine") or "").strip()
        if not title or not thesis or not narrative_spine:
            return {"accepted": False, "error": "title, thesis, and narrative_spine are required"}
        current["memo_fields"] = {
            "title": title[:240],
            "thesis": thesis[:1800],
            "narrative_spine": narrative_spine[:1800],
            "source_coverage": [str(item)[:400] for item in args.get("source_coverage") or [] if str(item).strip()][:40],
            "open_questions": [str(item)[:400] for item in args.get("open_questions") or [] if str(item).strip()][:40],
            "risk_register": [str(item)[:500] for item in args.get("risk_register") or [] if str(item).strip()][:40],
        }
        current["status"] = "finalized"
        await save_research_workspace(db, generation_run_id=run.id, workspace=current, status=V2ArtifactStatus.PASSED)
        return {"accepted": True, "status": "finalized", "atom_count": len(current.get("atom_drafts") or [])}

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
                            "required_atom_kinds": {"type": "array", "items": {"type": "string"}},
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
                            "open_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["summary", "open_questions"],
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
                            "kind": {"type": "string"},
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
            ]
        )
    if mode == "finalizer":
        tools.extend(
            [
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
                    name="finalize_research_memo",
                    description="Persist final memo fields. The backend assembles ResearchMemo atoms from durable atom drafts.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "thesis": {"type": "string"},
                            "narrative_spine": {"type": "string"},
                            "source_coverage": {"type": "array", "items": {"type": "string"}},
                            "open_questions": {"type": "array", "items": {"type": "string"}},
                            "risk_register": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": [
                            "title",
                            "thesis",
                            "narrative_spine",
                            "source_coverage",
                            "open_questions",
                            "risk_register",
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
    await save_research_workspace(db, generation_run_id=run.id, workspace=current)
    return current


async def research_workflow_guard_error(
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
    if read_count < RESEARCH_READS_PER_COMMIT:
        return None
    return {
        "accepted": False,
        "error": "research_job_requires_state_commit",
        "message": "Persist a research observation, atom draft, risk, or finish the job before more reading/searching.",
        "job_id": current_job_id,
        "read_calls_since_commit": read_count,
        "expected_next_actions": ["record_research_observation", "upsert_editorial_atom", "finish_research_job"],
    }


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
        "open_questions": [str(item)[:360] for item in (workspace.get("open_questions") or [])[:80]],
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
                "atom_count": len(item.get("atom_ids") or []),
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
            for item in (workspace.get("atom_drafts") or [])[:max_atoms]
        ],
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
        "open_questions": [str(item)[:360] for item in (workspace.get("open_questions") or [])[:80]],
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
            "Use atom_drafts as the memo backbone. Use observations to explain coverage, open questions, "
            "and risks. Do not invent atoms that were not persisted as atom_drafts."
        ),
    }


def research_job_prompt_payload(workspace: dict[str, Any], job_id: str, *, source_map_summary: dict[str, Any]) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    job = _workspace_job(workspace, job_id) or {}
    return {
        "job": job,
        "workspace_progress": research_workspace_summary_for_prompt(workspace, max_jobs=40, max_atoms=40),
        "source_map_summary": _source_map_summary_for_job(source_map_summary, job),
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
    for draft in workspace.get("atom_drafts") or []:
        evidence_ids = _dedupe([str(item) for item in draft.get("evidence_ids") or [] if str(item).strip()])
        anchor_refs = _dedupe([*(draft.get("anchor_refs") or []), *evidence_ids])
        atoms.append(
            EditorialAtom(
                kind=_atom_kind(str(draft.get("kind") or "")),  # type: ignore[arg-type]
                title=str(draft.get("title") or "Editorial atom")[:220],
                explanation=str(draft.get("explanation") or draft.get("claim") or "")[:1800],
                source_ids=_dedupe([str(item) for item in draft.get("source_ids") or [] if str(item).strip()]),
                quote_refs=[str(item) for item in draft.get("quote_refs") or [] if str(item).strip()][:8],
                story_value=str(draft.get("story_value") or "Use in treatment.")[:900],
                anchor_refs=anchor_refs[:12],
                evidence_ids=evidence_ids,
                confidence=_confidence(draft.get("confidence")),
                risk=str(draft.get("risk") or "medium")[:40] or "medium",
                source_coverage_status=_coverage_status(str(draft.get("source_coverage_status") or "")),  # type: ignore[arg-type]
            )
        )
    if not atoms:
        atoms = [
            EditorialAtom(
                kind="unanswered_question",
                title="Research did not produce atom drafts",
                explanation="No durable editorial atom drafts were saved in the research workspace.",
                story_value="Blocks confident treatment generation.",
                confidence=0.0,
                risk="high",
                source_coverage_status="weak",
            )
        ]
    processed_summaries = [str(item.get("summary")) for item in workspace.get("processed_jobs") or [] if item.get("summary")]
    risks = [
        str(item.get("risk") if isinstance(item, dict) else item)
        for item in workspace.get("risk_register") or []
        if str(item.get("risk") if isinstance(item, dict) else item).strip()
    ]
    return ResearchMemo(
        title=title,
        thesis=fields.get("thesis") or f"The episode should investigate {title} through source-grounded mechanisms, stakes, and turns.",
        narrative_spine=fields.get("narrative_spine") or "Move from core mechanism to origins, turning points, consequences, incentives, and unresolved risks.",
        editorial_atoms=atoms,
        source_coverage=(fields.get("source_coverage") or processed_summaries)[:40],
        open_questions=_dedupe([*(workspace.get("open_questions") or []), *(fields.get("open_questions") or [])])[:40],
        risk_register=_dedupe([*risks, *(fields.get("risk_register") or [])])[:40],
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
            **({"evidence_ids": card.evidence_ids[:8]} if include_evidence_ids else {}),
        }
        for card in source_map.source_cards[:80]
    ]
    quote_cards = [
        {
            "source_ref": source_ref_for_id(card.source_id, context),
            **({"evidence_id": card.evidence_id} if include_evidence_ids else {}),
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
            **({"evidence_ids": cluster.evidence_ids[:10]} if include_evidence_ids else {}),
            "rationale": cluster.rationale[:500],
        }
        for cluster in source_map.source_triage.source_clusters[:40]
    ]
    relationships = [
        {
            "source_ref": source_ref_for_id(relationship.source_id, context),
            "related_source_ref": source_ref_for_id(relationship.related_source_id, context),
            "relationship": relationship.relationship,
            **({"evidence_ids": relationship.evidence_ids[:10]} if include_evidence_ids else {}),
            "rationale": relationship.rationale[:400],
        }
        for relationship in source_map.source_triage.source_relationships[:60]
    ]
    return {
        "source_card_count": len(source_map.source_cards),
        "quote_card_count": len(source_map.quote_cards),
        "gap_count": len(source_map.gaps) + len(source_map.source_triage.gaps),
        "includes_evidence_ids": include_evidence_ids,
        "source_cards": source_cards,
        "quote_cards": quote_cards,
        "clusters": clusters,
        "relationships": relationships,
        "gaps": [*source_map.gaps[:20], *source_map.source_triage.gaps[:20]],
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


def _atom_kind(value: str) -> str:
    allowed = {
        "conflict",
        "mechanism",
        "history",
        "turning_point",
        "consequence",
        "exception",
        "human_cost",
        "institutional_incentive",
        "unanswered_question",
    }
    text = str(value or "").strip()
    return text if text in allowed else "unanswered_question"


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
    "ensure_research_workspace",
    "load_research_workspace",
    "load_research_workspace_artifact",
    "research_finalizer_payload_for_prompt",
    "research_job_prompt_payload",
    "research_memo_from_workspace",
    "research_workspace_summary_for_prompt",
    "research_workflow_guard_error",
    "save_research_workspace",
    "source_map_research_summary",
    "start_research_job",
    "unprocessed_research_jobs",
    "unresolved_research_jobs",
]
