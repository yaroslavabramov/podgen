from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.persistence_contracts import semantic_field_error
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
    source_map_research_summary,
)
from app.generation_v2.research_workspace_memo import research_memo_from_workspace
from app.generation_v2.research_workspace_persistence import (
    load_research_workspace,
    load_research_workspace_artifact,
    save_research_workspace,
)
from app.generation_v2.research_workspace_repair_queue import enqueue_research_repair_jobs
from app.generation_v2.research_workspace_gap_helpers import dedupe_gaps as _dedupe_gaps
from app.generation_v2.research_workspace_job_state import (
    invalid_job_evidence_ids as _invalid_job_evidence_ids,
    invalid_workspace_evidence_ids as _invalid_workspace_evidence_ids,
    invalid_workspace_refs as _invalid_workspace_refs,
    mark_research_progress as _mark_research_progress,
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
    now as _now,
    source_map_signature as _source_map_signature,
)
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceMap
from app.models.generation_run import GenerationRun



def _semantic_field_error(args: dict[str, Any], field_names: list[str]) -> dict[str, Any] | None:
    return semantic_field_error(args, SEMANTIC_FIELD_LIMITS, field_names)


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
    from app.generation_v2.research_workspace_tools import (
        build_research_workspace_tools as _build_research_workspace_tools,
    )

    return _build_research_workspace_tools(
        db=db,
        run=run,
        workspace=workspace,
        source_inventory=source_inventory,
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
    job["started_at"] = _now()
    job["updated_at"] = _now()
    _replace_workspace_job(current, job)
    current["current_job"] = {"job_id": job_id, "started_at": job["started_at"], "source_refs": job.get("source_refs") or []}
    _reset_research_read_advisory(current)
    await save_research_workspace(db, generation_run_id=run.id, workspace=current)
    return current


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
    "_semantic_field_error",
    "_source_ids_for_refs",
    "source_map_research_summary",
    "start_research_job",
    "unprocessed_research_jobs",
    "unresolved_research_jobs",
]
