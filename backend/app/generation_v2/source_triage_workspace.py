from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_artifact
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import SourceMap
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import RunCorpusView, SourceVersion, V2Artifact, V2ArtifactStatus, V2ArtifactType, V2ToolTrace


WORKSPACE_ARTIFACT_KEY = "main"
WORKSPACE_SCHEMA_VERSION = "source_triage_workspace.v1"
CAPSULE_SCHEMA_VERSION = "source_triage_capsule.v1"
TRIAGE_READ_TOOLS = {
    "search_corpus",
    "read_evidence",
    "read_source_window",
    "skim_source",
    "inspect_source",
    "compare_source_evidence",
}
TRIAGE_SEMANTIC_COMMIT_TOOLS = {
    "record_triage_observation",
    "finish_triage_source_set",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_source_cluster",
    "record_gap",
}
TRIAGE_READS_PER_COMMIT = 10
TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT = 8
TRIAGE_CAPSULE_PAGE_MAX_LIMIT = 12
TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS = 9000
TRIAGE_CAPSULE_PREVIEW_CHARS = 300


async def load_source_triage_workspace(db: AsyncSession, generation_run_id: uuid.UUID) -> dict[str, Any] | None:
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
            V2Artifact.artifact_key == WORKSPACE_ARTIFACT_KEY,
            V2Artifact.is_current.is_(True),
        )
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        return None
    return dict(artifact.payload or {})


async def save_source_triage_workspace(
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
        artifact_type=V2ArtifactType.SOURCE_TRIAGE_WORKSPACE,
        artifact_key=WORKSPACE_ARTIFACT_KEY,
        payload=workspace,
        status=status,
    )


async def ensure_source_triage_workspace(
    db: AsyncSession,
    *,
    run: GenerationRun,
    run_view: RunCorpusView,
    inventory: SourceMap,
) -> dict[str, Any]:
    existing = await load_source_triage_workspace(db, run.id)
    manifest_hash = str(run_view.manifest_hash or "")
    source_order = _source_order(inventory)
    if existing and existing.get("schema_version") == WORKSPACE_SCHEMA_VERSION and existing.get("manifest_hash") == manifest_hash:
        workspace = _normalize_workspace(existing)
        if workspace.get("source_order") == source_order:
            return workspace
    workspace = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "manifest_hash": manifest_hash,
        "status": "orientation",
        "source_order": source_order,
        "source_capsules": {},
        "source_sets": [],
        "current_set": None,
        "processed_sets": [],
        "deferred_sources": [],
        "mapped_sources": [],
        "open_questions": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=workspace)
    return workspace


def build_workspace_tools(
    *,
    db: AsyncSession,
    run: GenerationRun,
    workspace: dict[str, Any],
    inventory: SourceMap,
    evidence_toolset: Any | None = None,
    mode: str,
    current_set_id: str | None = None,
) -> list[AgentTool]:
    tools: list[AgentTool] = []

    async def list_source_capsules_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        source_order = [str(item) for item in current.get("source_order") or []]
        limit = max(1, min(int(args.get("limit") or TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT), TRIAGE_CAPSULE_PAGE_MAX_LIMIT))
        cursor = str(args.get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0
        refs = source_order[offset : offset + limit]
        if evidence_toolset is not None:
            current = await ensure_capsules_for_refs(
                db,
                run=run,
                workspace=current,
                inventory=inventory,
                evidence_toolset=evidence_toolset,
                source_refs=refs,
            )
        capsules = current.get("source_capsules") or {}
        page_capsules: list[dict[str, Any]] = []
        for ref in refs:
            capsule = capsules.get(ref) or _metadata_only_capsule(inventory, ref)
            candidate = [*page_capsules, capsule]
            encoded = json.dumps(
                _capsule_page_payload(
                    source_count=len(source_order),
                    capsules=candidate,
                    next_cursor=str(offset + len(candidate)) if offset + len(candidate) < len(source_order) else "",
                ),
                ensure_ascii=False,
                default=str,
            )
            if page_capsules and len(encoded) > TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS:
                break
            page_capsules = candidate
        returned_count = len(page_capsules)
        return {
            **_capsule_page_payload(
                source_count=len(source_order),
                capsules=page_capsules,
                next_cursor=str(offset + returned_count) if offset + returned_count < len(source_order) else "",
            ),
            "returned_count": returned_count,
            "page_limit": limit,
        }

    async def upsert_triage_source_set_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        source_order = set(str(item) for item in current.get("source_order") or [])
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {"accepted": False, "error": "source_refs must exist in the triage workspace", "invalid_source_refs": invalid}
        if not source_refs:
            return {"accepted": False, "error": "upsert_triage_source_set requires at least one source_ref"}
        label = str(args.get("label") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not label or not purpose:
            return {"accepted": False, "error": "label and purpose are required"}
        set_id = _stable_set_id(str(args.get("set_id") or ""), label, len(current.get("source_sets") or []) + 1)
        source_set = {
            "set_id": set_id,
            "label": label[:180],
            "purpose": purpose[:600],
            "source_refs": source_refs,
            "status": "planned",
            "rationale": str(args.get("rationale") or "")[:1200],
            "questions": [str(item)[:280] for item in args.get("questions") or [] if str(item).strip()][:8],
            "priority": str(args.get("priority") or "normal")[:50] or "normal",
            "findings_summary": "",
            "evidence_ids": [],
            "written_actions": [],
            "observations": [],
            "deferred_reason": "",
            "repair": bool(args.get("repair")),
            "updated_at": _now(),
        }
        sets = [item for item in current.get("source_sets") or [] if item.get("set_id") != set_id]
        sets.append(source_set)
        current["source_sets"] = sets
        current["status"] = "planning"
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "source_count": len(source_refs), "planned_set_count": len(sets)}

    async def finalize_triage_workspace_plan_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        source_order = [str(item) for item in current.get("source_order") or []]
        source_sets = [dict(item) for item in current.get("source_sets") or []]
        covered = {ref for item in source_sets for ref in item.get("source_refs") or []}
        deferred = []
        for item in args.get("deferred_sources") or []:
            if isinstance(item, dict):
                refs = [str(ref) for ref in item.get("source_refs") or [] if str(ref).strip()]
                reason = str(item.get("reason") or "").strip()
            else:
                refs = [str(item)]
                reason = str(args.get("deferred_reason") or "").strip()
            for ref in refs:
                if ref in source_order:
                    deferred.append({"source_ref": ref, "reason": reason or "Deferred by triage planner."})
        deferred_refs = {item["source_ref"] for item in deferred}
        missing = [ref for ref in source_order if ref not in covered and ref not in deferred_refs]
        if not source_sets:
            return {"accepted": False, "error": "Cannot finalize triage workspace without source sets"}
        if missing:
            return {
                "accepted": False,
                "error": "Every source_ref must be assigned to a source set or explicitly deferred",
                "missing_source_refs": missing[:80],
            }
        current["deferred_sources"] = _merge_deferred(current.get("deferred_sources") or [], deferred)
        current["status"] = "planned"
        current["plan_summary"] = str(args.get("summary") or "")[:1600]
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
        return {
            "accepted": True,
            "status": "planned",
            "source_set_count": len(source_sets),
            "deferred_source_count": len(current["deferred_sources"]),
        }

    async def record_triage_observation_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        set_id = str(args.get("set_id") or current_set_id or (current.get("current_set") or {}).get("set_id") or "")
        source_refs = _dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        if not set_id:
            return {"accepted": False, "error": "record_triage_observation requires a current set_id"}
        source_set = _workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        invalid = [ref for ref in source_refs if ref not in set(source_set.get("source_refs") or [])]
        if invalid:
            return {"accepted": False, "error": "Observation source_refs must belong to the current source set", "invalid_source_refs": invalid}
        note = str(args.get("note") or "").strip()
        if not note:
            return {"accepted": False, "error": "note is required"}
        evidence_ids = _dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        if evidence_ids and evidence_toolset is not None:
            invalid_evidence_ids: list[str] = []
            allowed_refs = set(source_set.get("source_refs") or [])
            for evidence_id in evidence_ids:
                evidence = await evidence_toolset._evidence_in_run(evidence_id)
                if evidence is None:
                    invalid_evidence_ids.append(evidence_id)
                    continue
                evidence_ref = await evidence_toolset._source_ref_for_id(evidence.source_id)
                if evidence_ref not in allowed_refs:
                    invalid_evidence_ids.append(evidence_id)
            if invalid_evidence_ids:
                return {
                    "accepted": False,
                    "error": "observation evidence_ids must be observed evidence from the current source set",
                    "invalid_evidence_ids": invalid_evidence_ids,
                }
        observation = {
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "note": note[:1600],
            "pending_questions": [str(item)[:300] for item in args.get("pending_questions") or [] if str(item).strip()][:8],
            "created_at": _now(),
        }
        source_set.setdefault("observations", []).append(observation)
        source_set["evidence_ids"] = _dedupe([*(source_set.get("evidence_ids") or []), *evidence_ids])
        source_set["updated_at"] = _now()
        _replace_workspace_set(current, source_set)
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "observation_count": len(source_set.get("observations") or []), "evidence_count": len(evidence_ids)}

    async def finish_triage_source_set_tool(args: dict[str, Any]) -> dict[str, Any]:
        current = await load_source_triage_workspace(db, run.id) or workspace
        set_id = str(args.get("set_id") or current_set_id or (current.get("current_set") or {}).get("set_id") or "")
        source_set = _workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        status = str(args.get("status") or "").strip()
        if status not in {"mapped", "deferred", "needs_repair"}:
            return {"accepted": False, "error": "status must be mapped, deferred, or needs_repair"}
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"accepted": False, "error": "summary is required"}
        if status == "mapped":
            has_semantic_state = bool(source_set.get("observations") or source_set.get("written_actions") or source_set.get("evidence_ids"))
            if not has_semantic_state:
                return {"accepted": False, "error": "mapped set requires at least one observation, write action, or evidence_id"}
        if status in {"deferred", "needs_repair"} and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred/needs_repair status"}
        source_set["status"] = status
        source_set["findings_summary"] = summary[:1600]
        source_set["deferred_reason"] = str(args.get("deferred_reason") or "")[:1000]
        source_set["updated_at"] = _now()
        _replace_workspace_set(current, source_set)
        processed = [item for item in current.get("processed_sets") or [] if item.get("set_id") != set_id]
        processed.append({"set_id": set_id, "status": status, "summary": source_set["findings_summary"], "updated_at": _now()})
        current["processed_sets"] = processed
        current["current_set"] = None
        if status == "deferred":
            current["deferred_sources"] = _merge_deferred(
                current.get("deferred_sources") or [],
                [{"source_ref": ref, "reason": source_set["deferred_reason"] or summary} for ref in source_set.get("source_refs") or []],
            )
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "status": status, "processed_set_count": len(processed)}

    if mode == "planner":
        tools.extend(
            [
                AgentTool(
                    name="list_source_capsules",
                    description="List compact orientation capsules for source_refs. Capsules contain neutral readable hints, not source-role decisions.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer", "minimum": 1, "maximum": TRIAGE_CAPSULE_PAGE_MAX_LIMIT},
                            "cursor": {"type": "string"},
                        },
                        "required": ["limit", "cursor"],
                    },
                    handler=list_source_capsules_tool,
                ),
                AgentTool(
                    name="upsert_triage_source_set",
                    description="Persist one planned source set for later detailed triage. Use this to save grouping decisions as you page through capsules.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "set_id": {"type": "string"},
                            "label": {"type": "string"},
                            "purpose": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "rationale": {"type": "string"},
                            "questions": {"type": "array", "items": {"type": "string"}},
                            "priority": {"type": "string"},
                            "repair": {"type": "boolean"},
                        },
                        "required": ["label", "purpose", "source_refs", "rationale", "questions", "priority"],
                    },
                    handler=upsert_triage_source_set_tool,
                ),
                AgentTool(
                    name="finalize_triage_workspace_plan",
                    description="Mark the source-set plan complete. Every source_ref must be in a set or explicitly deferred.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "deferred_sources": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "source_refs": {"type": "array", "items": {"type": "string"}},
                                        "reason": {"type": "string"},
                                    },
                                    "required": ["source_refs", "reason"],
                                    "additionalProperties": False,
                                },
                            },
                            "deferred_reason": {"type": "string"},
                        },
                        "required": ["summary", "deferred_sources", "deferred_reason"],
                    },
                    handler=finalize_triage_workspace_plan_tool,
                ),
            ]
        )
    if mode == "set":
        tools.extend(
            [
                AgentTool(
                    name="record_triage_observation",
                    description="Persist a compact intermediate finding for the current source set before reading more or crossing a checkpoint boundary.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "set_id": {"type": "string"},
                            "source_refs": {"type": "array", "items": {"type": "string"}},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                            "note": {"type": "string"},
                            "pending_questions": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["set_id", "source_refs", "evidence_ids", "note", "pending_questions"],
                    },
                    handler=record_triage_observation_tool,
                ),
                AgentTool(
                    name="finish_triage_source_set",
                    description="Finish the current source set after writing source cards/clusters/relationships/gaps or explicitly deferring it.",
                    parameters={
                        "type": "object",
                        "properties": {
                            "set_id": {"type": "string"},
                            "status": {"type": "string", "enum": ["mapped", "deferred", "needs_repair"]},
                            "summary": {"type": "string"},
                            "deferred_reason": {"type": "string"},
                        },
                        "required": ["set_id", "status", "summary", "deferred_reason"],
                    },
                    handler=finish_triage_source_set_tool,
                ),
            ]
        )
    if mode == "finalizer":
        tools.append(
            AgentTool(
                name="finish_triage_source_set",
                description="Mark a repair source set as deferred/needs_repair when finalization cannot proceed.",
                parameters={
                    "type": "object",
                    "properties": {
                        "set_id": {"type": "string"},
                        "status": {"type": "string", "enum": ["mapped", "deferred", "needs_repair"]},
                        "summary": {"type": "string"},
                        "deferred_reason": {"type": "string"},
                    },
                    "required": ["set_id", "status", "summary", "deferred_reason"],
                },
                handler=finish_triage_source_set_tool,
            )
        )
    return tools


async def ensure_capsules_for_refs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    workspace: dict[str, Any],
    inventory: SourceMap,
    evidence_toolset: Any,
    source_refs: list[str],
) -> dict[str, Any]:
    current = _normalize_workspace(workspace)
    capsules = dict(current.get("source_capsules") or {})
    context = build_source_ref_context(inventory)
    by_ref = {source_ref_for_id(card.source_id, context): card for card in inventory.source_cards}
    changed = False
    for ref in source_refs:
        if ref in capsules:
            continue
        card = by_ref.get(ref)
        if card is None:
            continue
        capsules[ref] = await _build_capsule_for_card(evidence_toolset, card=card, source_ref=ref)
        changed = True
    if changed:
        current["source_capsules"] = capsules
        await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
    return current


async def start_triage_source_set(
    db: AsyncSession,
    *,
    run: GenerationRun,
    workspace: dict[str, Any],
    set_id: str,
    inventory: SourceMap,
    evidence_toolset: Any,
) -> dict[str, Any]:
    current = await load_source_triage_workspace(db, run.id) or workspace
    source_set = _workspace_set(current, set_id)
    if source_set is None:
        raise RuntimeError(f"Unknown source triage set: {set_id}")
    current = await ensure_capsules_for_refs(
        db,
        run=run,
        workspace=current,
        inventory=inventory,
        evidence_toolset=evidence_toolset,
        source_refs=[str(item) for item in source_set.get("source_refs") or []],
    )
    source_set = _workspace_set(current, set_id) or source_set
    source_set["status"] = "in_progress"
    source_set["started_at"] = _now()
    source_set["updated_at"] = _now()
    _replace_workspace_set(current, source_set)
    current["current_set"] = {"set_id": set_id, "started_at": source_set["started_at"], "source_refs": source_set.get("source_refs") or []}
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
    return current


async def mark_workspace_write_action(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    tool_name: str,
    source_refs: list[str],
    evidence_ids: list[str],
) -> None:
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace:
        return
    current_set = workspace.get("current_set") or {}
    set_id = str(current_set.get("set_id") or "")
    source_set = _workspace_set(workspace, set_id) if set_id else None
    if source_set is None:
        return
    action = {
        "tool_name": tool_name,
        "source_refs": _dedupe(source_refs),
        "evidence_ids": _dedupe(evidence_ids),
        "created_at": _now(),
    }
    source_set.setdefault("written_actions", []).append(action)
    source_set["evidence_ids"] = _dedupe([*(source_set.get("evidence_ids") or []), *evidence_ids])
    source_set["updated_at"] = _now()
    _replace_workspace_set(workspace, source_set)
    workspace["mapped_sources"] = _dedupe([*(workspace.get("mapped_sources") or []), *source_refs])
    await save_source_triage_workspace(db, generation_run_id=generation_run_id, workspace=workspace)


async def triage_workflow_guard_error(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    current_set_id: str | None,
) -> dict[str, Any] | None:
    if not current_set_id:
        return None
    workspace = await load_source_triage_workspace(db, generation_run_id)
    current_set = (workspace or {}).get("current_set") or {}
    started_at = _parse_dt(current_set.get("started_at"))
    if started_at is None:
        return None
    commit_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.created_at >= started_at,
            V2ToolTrace.tool_name.in_(list(TRIAGE_SEMANTIC_COMMIT_TOOLS)),
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
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.created_at >= since,
            V2ToolTrace.tool_name.in_(list(TRIAGE_READ_TOOLS)),
        )
    )
    read_count = len(list(read_result.scalars()))
    if read_count < TRIAGE_READS_PER_COMMIT:
        return None
    return {
        "accepted": False,
        "error": "source_set_requires_state_commit",
        "message": "You have read/searched enough in this source set. Call record_triage_observation, a SourceMap write tool, or finish_triage_source_set before more reading/searching.",
        "set_id": current_set_id,
        "read_calls_since_commit": read_count,
        "expected_next_actions": [
            "record_triage_observation",
            "upsert_source_card",
            "record_source_cluster",
            "record_source_relationship",
            "record_gap",
            "finish_triage_source_set",
        ],
    }


def workspace_summary_for_prompt(workspace: dict[str, Any], *, max_sets: int = 30) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    return {
        "status": workspace.get("status"),
        "source_count": len(workspace.get("source_order") or []),
        "source_set_count": len(workspace.get("source_sets") or []),
        "current_set": workspace.get("current_set"),
        "processed_sets": workspace.get("processed_sets") or [],
        "mapped_sources": workspace.get("mapped_sources") or [],
        "deferred_sources": workspace.get("deferred_sources") or [],
        "open_questions": workspace.get("open_questions") or [],
        "source_sets": [
            {
                "set_id": item.get("set_id"),
                "label": item.get("label"),
                "purpose": item.get("purpose"),
                "source_refs": item.get("source_refs") or [],
                "status": item.get("status"),
                "priority": item.get("priority"),
                "findings_summary": item.get("findings_summary"),
                "observation_count": len(item.get("observations") or []),
                "written_action_count": len(item.get("written_actions") or []),
                "deferred_reason": item.get("deferred_reason"),
            }
            for item in (workspace.get("source_sets") or [])[:max_sets]
        ],
    }


def unresolved_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "in_progress", "needs_repair"}
    ]


def unresolved_source_set_summary(workspace: dict[str, Any], *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "set_id": item.get("set_id"),
            "label": item.get("label"),
            "status": item.get("status"),
            "source_refs": item.get("source_refs") or [],
            "deferred_reason": item.get("deferred_reason") or "",
        }
        for item in unresolved_source_sets(workspace)[:limit]
    ]


def source_set_prompt_payload(workspace: dict[str, Any], set_id: str) -> dict[str, Any]:
    workspace = _normalize_workspace(workspace)
    source_set = _workspace_set(workspace, set_id) or {}
    capsules = workspace.get("source_capsules") or {}
    return {
        "set": {
            "set_id": source_set.get("set_id"),
            "label": source_set.get("label"),
            "purpose": source_set.get("purpose"),
            "source_refs": source_set.get("source_refs") or [],
            "rationale": source_set.get("rationale"),
            "questions": source_set.get("questions") or [],
            "priority": source_set.get("priority"),
            "observations": source_set.get("observations") or [],
            "written_actions": source_set.get("written_actions") or [],
        },
        "capsules": [capsules.get(ref) for ref in source_set.get("source_refs") or [] if capsules.get(ref)],
        "workspace_progress": workspace_summary_for_prompt(workspace, max_sets=40),
    }


def unprocessed_source_sets(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _normalize_workspace(workspace).get("source_sets") or []
        if item.get("status") in {"planned", "needs_repair"}
    ]


def create_repair_sets_from_gate(
    workspace: dict[str, Any],
    *,
    gate: Any,
    inventory: SourceMap,
    source_map: SourceMap | None = None,
) -> dict[str, Any]:
    context = build_source_ref_context(inventory)
    source_ids: set[str] = set()
    for key in (
        "missing_source_card_evidence_source_ids",
        "missing_model_read_source_ids",
        "missing_source_card_write_source_ids",
        "invalid_source_ids",
    ):
        source_ids.update(str(item) for item in getattr(gate, key, []) or [])
    if source_map is not None:
        source_ids.update(_source_ids_for_invalid_evidence(source_map, set(getattr(gate, "invalid_evidence_ids", []) or [])))
        source_ids.update(_source_ids_for_ungrounded_items(source_map, getattr(gate, "ungrounded_relationships", []) or []))
        if getattr(gate, "unpersisted_completion_gap_notes", None):
            source_ids.update(card.source_id for card in source_map.source_cards[:6])
        if getattr(gate, "checked_evidence_count", 0) == 0:
            source_ids.update(card.source_id for card in source_map.source_cards[:6])
    source_order = {str(ref) for ref in workspace.get("source_order") or _source_order(inventory)}
    refs = _dedupe(
        [
            ref
            for source_id in source_ids
            if source_id and (ref := source_ref_for_id(source_id, context)) in source_order
        ]
    )
    if not refs and "finalize" not in str(getattr(gate, "summary", "")).lower():
        refs = [str(ref) for ref in (workspace.get("mapped_sources") or workspace.get("source_order") or [])[:6]]
    if not refs:
        return workspace
    repair_idx = 1 + len([item for item in workspace.get("source_sets") or [] if str(item.get("set_id", "")).startswith("repair_")])
    repair_set = {
        "set_id": f"repair_{repair_idx}",
        "label": f"Repair source triage gate issues {repair_idx}",
        "purpose": str(getattr(gate, "summary", "Repair source triage gate issues."))[:600],
        "source_refs": refs,
        "status": "planned",
        "rationale": "Created from source triage gate failure.",
        "questions": _gate_repair_questions(gate),
        "priority": "repair",
        "findings_summary": "",
        "evidence_ids": [],
        "written_actions": [],
        "observations": [],
        "deferred_reason": "",
        "repair": True,
        "updated_at": _now(),
    }
    workspace["source_sets"] = [*(workspace.get("source_sets") or []), repair_set]
    return workspace


def _gate_repair_questions(gate: Any) -> list[str]:
    questions = [str(getattr(gate, "summary", ""))[:280]]
    for label, values in (
        ("invalid evidence", getattr(gate, "invalid_evidence_ids", []) or []),
        ("ungrounded relationship/cluster", getattr(gate, "ungrounded_relationships", []) or []),
        ("unpersisted gap note", getattr(gate, "unpersisted_completion_gap_notes", []) or []),
        ("missing function evidence", getattr(gate, "missing_function_evidence", []) or []),
    ):
        if values:
            questions.append(f"Repair {label}: {', '.join(str(item) for item in values[:6])}"[:280])
    return [question for question in questions if question][:8]


def _source_ids_for_invalid_evidence(source_map: SourceMap, invalid_evidence_ids: set[str]) -> set[str]:
    if not invalid_evidence_ids:
        return set()
    source_ids: set[str] = set()
    for card in source_map.source_cards:
        if invalid_evidence_ids.intersection(card.evidence_ids):
            source_ids.add(card.source_id)
    for quote in source_map.quote_cards:
        if quote.evidence_id in invalid_evidence_ids:
            source_ids.add(quote.source_id)
    for cluster in source_map.source_triage.source_clusters:
        if invalid_evidence_ids.intersection(cluster.evidence_ids):
            source_ids.update(cluster.source_ids)
    for relation in source_map.source_triage.source_relationships:
        if invalid_evidence_ids.intersection(relation.evidence_ids):
            source_ids.add(relation.source_id)
            source_ids.add(relation.related_source_id)
    return source_ids


def _source_ids_for_ungrounded_items(source_map: SourceMap, ungrounded_items: list[str]) -> set[str]:
    source_ids: set[str] = set()
    clusters_by_id = {cluster.cluster_id: cluster for cluster in source_map.source_triage.source_clusters}
    for item in ungrounded_items:
        text = str(item or "")
        if text.startswith("cluster:"):
            cluster = clusters_by_id.get(text.split(":", 1)[1])
            if cluster:
                source_ids.update(cluster.source_ids)
            continue
        parts = text.split(":")
        if parts and parts[0]:
            source_ids.add(parts[0])
        if len(parts) >= 3 and parts[-1]:
            source_ids.add(parts[-1])
    return source_ids


async def _build_capsule_for_card(evidence_toolset: Any, *, card: Any, source_ref: str) -> dict[str, Any]:
    version = await evidence_toolset._resolve_source_version(
        source_id=str(card.source_id),
        source_version_id="",
        source_ref="",
        version_ref="",
    )
    warnings = [str(item) for item in getattr(card, "gaps_or_cautions", []) or [] if str(item).strip()]
    body: dict[str, Any] | None = None
    if version is not None:
        cache = (version.metadata_ or {}).get("v2_triage_capsule") or {}
        cache_key = _capsule_cache_key(version)
        if cache.get("schema_version") == CAPSULE_SCHEMA_VERSION and cache.get("cache_key") == cache_key:
            body = dict(cache.get("body") or {})
        if body is None:
            windows = await evidence_toolset._source_windows(version, max_windows=3)
            body = {
                "schema_version": CAPSULE_SCHEMA_VERSION,
                "cache_key": cache_key,
                "outline": [str(item.get("locator_text") or "")[:100] for item in windows if item.get("locator_text")][:4],
                "windows": [
                    {
                        "locator": str(item.get("locator_text") or "")[:100],
                        "locator_type": str(item.get("locator_type") or "unknown"),
                        "preview_text": " ".join(str(item.get("snippet_text") or "").split())[:TRIAGE_CAPSULE_PREVIEW_CHARS],
                    }
                    for item in windows[:2]
                    if str(item.get("snippet_text") or "").strip()
                ],
                "readability_warnings": warnings[:4],
                "readable": bool(windows),
            }
            version.metadata_ = {
                **(version.metadata_ or {}),
                "v2_triage_capsule": {
                    "schema_version": CAPSULE_SCHEMA_VERSION,
                    "cache_key": cache_key,
                    "body": body,
                    "updated_at": _now(),
                },
            }
            await evidence_toolset.db.flush()
    if body is None:
        body = {
            "schema_version": CAPSULE_SCHEMA_VERSION,
            "cache_key": "",
            "outline": [],
            "windows": [],
            "readability_warnings": [*warnings, "No readable local windows were available for orientation."],
            "readable": False,
        }
    return {
        "source_ref": source_ref,
        "title": str(getattr(card, "title", "") or "")[:180],
        "domain": getattr(card, "domain", None),
        "source_type": getattr(card, "source_type", None),
        "published_at": getattr(card, "published_at", None),
        "coverage_status": getattr(card, "coverage_status", None),
        "summary_hint": " ".join(str(getattr(card, "summary", "") or "").split())[:180],
        "outline": body.get("outline") or [],
        "windows": body.get("windows") or [],
        "readability_warnings": body.get("readability_warnings") or [],
        "readable": bool(body.get("readable")),
        "capsule_cache_hit": bool((version is not None) and body.get("cache_key") == ((version.metadata_ or {}).get("v2_triage_capsule") or {}).get("cache_key")),
    }


def _metadata_only_capsule(inventory: SourceMap, source_ref: str) -> dict[str, Any]:
    context = build_source_ref_context(inventory)
    for card in inventory.source_cards:
        if source_ref_for_id(card.source_id, context) == source_ref:
            return {
                "source_ref": source_ref,
                "title": card.title[:180],
                "domain": card.domain,
                "source_type": card.source_type,
                "published_at": card.published_at,
                "summary_hint": " ".join(card.summary.split())[:180],
                "outline": [],
                "windows": [],
                "readability_warnings": ["Capsule has not been built yet."],
                "readable": False,
            }
    return {"source_ref": source_ref, "readable": False, "windows": [], "outline": []}


def _source_order(inventory: SourceMap) -> list[str]:
    context = build_source_ref_context(inventory)
    return [source_ref_for_id(card.source_id, context) for card in inventory.source_cards]


def _capsule_page_payload(*, source_count: int, capsules: list[dict[str, Any]], next_cursor: str) -> dict[str, Any]:
    return {
        "accepted": True,
        "source_count": source_count,
        "capsules": capsules,
        "next_cursor": next_cursor,
    }


def _normalize_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(workspace or {})
    workspace.setdefault("schema_version", WORKSPACE_SCHEMA_VERSION)
    workspace.setdefault("source_order", [])
    workspace.setdefault("source_capsules", {})
    workspace.setdefault("source_sets", [])
    workspace.setdefault("processed_sets", [])
    workspace.setdefault("deferred_sources", [])
    workspace.setdefault("mapped_sources", [])
    workspace.setdefault("open_questions", [])
    return workspace


def _workspace_set(workspace: dict[str, Any], set_id: str) -> dict[str, Any] | None:
    for item in workspace.get("source_sets") or []:
        if item.get("set_id") == set_id:
            return dict(item)
    return None


def _replace_workspace_set(workspace: dict[str, Any], source_set: dict[str, Any]) -> None:
    set_id = source_set.get("set_id")
    workspace["source_sets"] = [source_set if item.get("set_id") == set_id else item for item in workspace.get("source_sets") or []]


def _stable_set_id(requested: str, label: str, index: int) -> str:
    if requested:
        return "".join(ch.lower() if ch.isalnum() else "_" for ch in requested)[:48].strip("_") or f"set_{index}"
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in label.strip())[:44].strip("_")
    return slug or f"set_{index}"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _merge_deferred(existing: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ref = {str(item.get("source_ref")): dict(item) for item in existing if item.get("source_ref")}
    for item in new_items:
        ref = str(item.get("source_ref") or "")
        if ref:
            by_ref[ref] = {"source_ref": ref, "reason": str(item.get("reason") or "")[:1000]}
    return list(by_ref.values())


def _capsule_cache_key(version: SourceVersion) -> str:
    return f"{version.id}:{version.content_hash or ''}:{version.updated_at.isoformat() if version.updated_at else ''}:{CAPSULE_SCHEMA_VERSION}"


def _now() -> str:
    return datetime.utcnow().isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


__all__ = [
    "build_workspace_tools",
    "create_repair_sets_from_gate",
    "ensure_capsules_for_refs",
    "ensure_source_triage_workspace",
    "load_source_triage_workspace",
    "mark_workspace_write_action",
    "save_source_triage_workspace",
    "source_set_prompt_payload",
    "start_triage_source_set",
    "triage_workflow_guard_error",
    "unprocessed_source_sets",
    "unresolved_source_set_summary",
    "unresolved_source_sets",
    "workspace_summary_for_prompt",
]
