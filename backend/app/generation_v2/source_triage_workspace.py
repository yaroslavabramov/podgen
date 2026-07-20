from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.runtime import AgentTool
from app.generation_v2.corpus_scope import scope_for_run_view
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_capsules import (
    build_capsule_for_card as _build_capsule_for_card,
    capsule_page_payload as _capsule_page_payload,
    metadata_only_capsule as _metadata_only_capsule,
    source_order as _source_order,
)
from app.generation_v2.source_triage_workspace_state import (
    WORKSPACE_SCHEMA_VERSION,
    dedupe as _dedupe,
    merge_deferred as _merge_deferred,
    normalize_workspace as _normalize_workspace,
    now as _now,
    replace_workspace_set as _replace_workspace_set,
    stable_set_id as _stable_set_id,
    source_set_covered_refs as _source_set_covered_refs,
    source_set_has_unresolved_refs as _source_set_has_unresolved_refs,
    unresolved_source_refs,
    workspace_set as _workspace_set,
)
from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_prompting import (
    planner_source_catalog,
    source_set_prompt_payload,
    unprocessed_source_sets,
    unresolved_source_set_summary,
    unresolved_source_sets,
    workspace_summary_for_prompt,
)
from app.generation_v2.source_triage_workspace_repair import create_repair_sets_from_gate
from app.generation_v2.source_triage_repair_state import (
    carry_pending_triage_repair,
    reconcile_carried_triage_evidence,
    triage_workspace_evidence_ids,
)
from app.generation_v2.source_triage_read_checkpointing import (
    clear_pending_exact_read,
    pending_exact_read_source_refs,
    record_triage_exact_read,
)
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_source_map_action_helpers import trace_accepted
from app.generation_v2.tools.evidence_types import (
    EVIDENCE_STATUS_CITED,
    EVIDENCE_STATUS_SELECTED,
    _WRITE_TOOL_NAMES,
)
from app.generation_v2.source_triage_workspace_tracking import (
    mark_workspace_write_action,
    triage_workflow_guard_error,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceRetrievalChannel,
    RunCorpusView,
    SourceVersion,
    V2ToolTrace,
)


async def ensure_source_triage_workspace(
    db: AsyncSession,
    *,
    run: GenerationRun,
    run_view: RunCorpusView,
    inventory: SourceMap,
) -> dict[str, Any]:
    existing = await load_source_triage_workspace(db, run.id)
    scope = scope_for_run_view(run_view)
    manifest_hash = str(run_view.manifest_hash or "")
    source_order = _source_order(inventory)
    legacy_workspace_contract = bool(
        existing
        and str(existing.get("schema_version") or "") != WORKSPACE_SCHEMA_VERSION
    )
    if (
        legacy_workspace_contract
        and int(existing.get("corpus_revision") or 1) == scope.revision
        and int(existing.get("attempt") or 1) == scope.attempt
    ):
        raise RuntimeError(
            "Legacy source-triage workspace requires a fresh corpus attempt before V3 triage"
        )
    if (
        existing
        and existing.get("schema_version") == WORKSPACE_SCHEMA_VERSION
        and existing.get("manifest_hash") == manifest_hash
        and int(existing.get("corpus_revision") or 1) == scope.revision
        and int(existing.get("attempt") or 1) == scope.attempt
    ):
        workspace = _normalize_workspace(existing)
        if workspace.get("source_order") == source_order:
            return workspace
    run_config = getattr(run, "config", None) or {}
    workspace = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "_workspace_revision": int((existing or {}).get("_workspace_revision") or 0),
        "manifest_hash": manifest_hash,
        "corpus_revision": scope.revision,
        "attempt": scope.attempt,
        "status": "orientation",
        "source_order": source_order,
        "source_capsules": {},
        "planner_inspected_source_refs": [],
        "intent": {
            "user_prompt": str(run_config.get("user_prompt") or ""),
            "language": str(run_config.get("language") or ""),
            "duration_seconds": int(run_config.get("duration_target") or 0),
        },
        "coverage_questions": [],
        "source_assessments": [],
        "source_findings": [],
        "source_read_receipts": [],
        "reading_notes": [],
        "coverage_outcomes": [],
        "coverage_syntheses": [],
        "triage_semantic_revision": 0,
        "source_sets": [],
        "current_set": None,
        "processed_sets": [],
        "deferred_sources": [],
        "mapped_sources": [],
        "open_questions": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    # Findings cannot be reconstructed from pre-V3 cards/observations. A
    # legacy workspace therefore starts clean in its already-created fresh
    # attempt; only a V3 targeted repair may carry durable semantic state.
    if not legacy_workspace_contract:
        workspace = carry_pending_triage_repair(
            existing or {},
            workspace,
            source_order=source_order,
        )
    if workspace.get("repair_operation") and int((existing or {}).get("attempt") or 1) != scope.attempt:
        evidence_id_map, invalid_evidence_ids = await _carry_evidence_to_current_attempt(
            db,
            run=run,
            run_view=run_view,
            workspace=workspace,
            source_attempt=int((existing or {}).get("attempt") or 1),
        )
        workspace = reconcile_carried_triage_evidence(
            workspace,
            evidence_id_map=evidence_id_map,
            invalid_evidence_ids=invalid_evidence_ids,
        )
        await _carry_source_map_write_actions_to_current_attempt(
            db,
            run=run,
            run_view=run_view,
            source_attempt=int((existing or {}).get("attempt") or 1),
            evidence_id_map=evidence_id_map,
        )
    await save_source_triage_workspace(
        db,
        generation_run_id=run.id,
        workspace=workspace,
        allow_scope_transition=True,
    )
    return workspace


async def _carry_evidence_to_current_attempt(
    db: AsyncSession,
    *,
    run: GenerationRun,
    run_view: RunCorpusView,
    workspace: dict[str, Any],
    source_attempt: int,
) -> tuple[dict[str, str], set[str]]:
    evidence_ids = triage_workspace_evidence_ids(workspace)
    parsed_ids: dict[str, uuid.UUID] = {}
    invalid = set(evidence_ids)
    for evidence_id in evidence_ids:
        try:
            parsed_ids[evidence_id] = uuid.UUID(evidence_id)
        except (TypeError, ValueError):
            continue
    if not parsed_ids:
        return {}, invalid

    result = await db.execute(select(EvidenceItem).where(EvidenceItem.id.in_(list(parsed_ids.values()))))
    evidence_by_id = {str(item.id): item for item in result.scalars()}
    version_ids = {item.source_version_id for item in evidence_by_id.values()}
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.id.in_(version_ids)))
    versions_by_id = {str(item.id): item for item in version_result.scalars()}
    scope = scope_for_run_view(run_view)
    selected_source_ids = {str(item) for item in run_view.selected_source_ids}
    selected_version_ids = {str(item) for item in run_view.selected_source_version_ids}
    evidence_id_map: dict[str, str] = {}

    for evidence_id in evidence_ids:
        item = evidence_by_id.get(evidence_id)
        version = versions_by_id.get(str(item.source_version_id)) if item is not None else None
        usage_status = str((item.metadata_ or {}).get("usage_status") or "") if item is not None else ""
        if (
            item is None
            or version is None
            or item.generation_run_id != run.id
            or item.project_id != run.project_id
            or int(item.corpus_revision or 1) != scope.revision
            or int(item.attempt or 1) != source_attempt
            or source_attempt >= scope.attempt
            or str(item.source_id) not in selected_source_ids
            or str(item.source_version_id) not in selected_version_ids
            or version.source_id != item.source_id
            or version.project_id != run.project_id
            or usage_status not in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
            or not str(item.snippet_text or "").strip()
        ):
            continue

        previous_metadata = dict(item.metadata_ or {})
        previous_metadata.pop("cited", None)
        previous_lineage = previous_metadata.get("attempt_lineage")
        root_evidence_id = (
            str(previous_lineage.get("root_evidence_id") or "") if isinstance(previous_lineage, dict) else ""
        ) or evidence_id
        origin_retrieval_channel = str(previous_metadata.get("origin_retrieval_channel") or item.retrieval_channel)
        lineage = {
            "kind": "immutable_source_version_carry",
            "root_evidence_id": root_evidence_id,
            "parent_evidence_id": evidence_id,
            "from_attempt": source_attempt,
            "to_attempt": scope.attempt,
            "corpus_revision": scope.revision,
            "source_version_id": str(item.source_version_id),
            "selected_source_version_verified": True,
            "retrieval_performed": False,
        }
        carried = await persist_evidence_item(
            db,
            generation_run_id=run.id,
            project_id=run.project_id,
            source_id=item.source_id,
            source_version_id=item.source_version_id,
            provider=item.provider,
            provider_vector_store_id=item.provider_vector_store_id,
            provider_file_id=item.provider_file_id,
            provider_vector_store_file_id=item.provider_vector_store_file_id,
            retrieval_channel=EvidenceRetrievalChannel.ATTEMPT_LINEAGE,
            query_text=None,
            purpose="triage_attempt_lineage",
            search_result_index=None,
            score=item.score,
            snippet_text=item.snippet_text,
            normalized_quote_text=item.normalized_quote_text,
            locator_text=item.locator_text,
            locator_type=item.locator_type,
            evidence_strength=item.evidence_strength,
            metadata={
                **previous_metadata,
                "usage_status": EVIDENCE_STATUS_SELECTED,
                "origin_retrieval_channel": origin_retrieval_channel,
                "attempt_lineage": lineage,
            },
        )
        carried.metadata_ = {
            **(carried.metadata_ or {}),
            "usage_status": EVIDENCE_STATUS_SELECTED,
            "origin_retrieval_channel": origin_retrieval_channel,
            "attempt_lineage": lineage,
        }
        evidence_id_map[evidence_id] = str(carried.id)
        invalid.discard(evidence_id)
    await db.flush()
    return evidence_id_map, invalid


async def _carry_source_map_write_actions_to_current_attempt(
    db: AsyncSession,
    *,
    run: GenerationRun,
    run_view: RunCorpusView,
    source_attempt: int,
    evidence_id_map: dict[str, str],
) -> int:
    """Copy accepted semantic SourceMap writes into a repair attempt.

    Tool traces are the authoritative payload for cards, quotes, clusters,
    relationships, and gaps. Workspace write summaries are intentionally too
    small to reconstruct them.
    """

    scope = scope_for_run_view(run_view)
    if source_attempt >= scope.attempt:
        return 0
    result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == run.id,
            V2ToolTrace.corpus_revision == scope.revision,
            V2ToolTrace.attempt == source_attempt,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_WRITE_TOOL_NAMES)),
        )
        .order_by(V2ToolTrace.created_at.asc(), V2ToolTrace.id.asc())
    )
    source_traces = [
        trace
        for trace in result.scalars()
        if trace_accepted(trace) and str(trace.tool_name or "") != "finalize_source_map"
    ]
    if not source_traces:
        return 0

    marker_by_source_id = {
        str(trace.id): f"triage-action-lineage:{source_attempt}:{trace.id}"
        for trace in source_traces
    }
    existing_result = await db.execute(
        select(V2ToolTrace).where(
            V2ToolTrace.generation_run_id == run.id,
            V2ToolTrace.corpus_revision == scope.revision,
            V2ToolTrace.attempt == scope.attempt,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.provider_call_id.in_(list(marker_by_source_id.values())),
        )
    )
    existing_by_marker = {
        str(trace.provider_call_id or ""): trace
        for trace in existing_result.scalars()
        if str(trace.provider_call_id or "")
    }

    carried_count = 0
    for source_trace in source_traces:
        source_trace_id = str(source_trace.id)
        marker = marker_by_source_id[source_trace_id]
        existing_trace = existing_by_marker.get(marker)
        action = dict(source_trace.input_json or {})
        action_evidence_ids = set(_source_map_action_evidence_ids(action))
        unavailable_evidence_ids = sorted(action_evidence_ids - set(evidence_id_map))
        lineage = {
            "kind": "source_map_action_carry",
            "source_trace_id": source_trace_id,
            "from_attempt": source_attempt,
            "to_attempt": scope.attempt,
            "corpus_revision": scope.revision,
        }
        if unavailable_evidence_ids:
            if existing_trace is not None:
                existing_trace.output_json = {
                    "accepted": False,
                    "error": "carried_action_evidence_unavailable",
                    "unavailable_evidence_count": len(unavailable_evidence_ids),
                    "_attempt_lineage": lineage,
                }
            continue

        carried_input = _remap_source_map_action_evidence(action, evidence_id_map)
        if str(source_trace.tool_name or "") == "resolve_source_map_repair":
            # The accepted repair target is still the same record, but its
            # fingerprint can change when evidence ids are remapped.
            carried_input["target_fingerprint"] = ""
        carried_output = _remap_source_map_action_evidence(
            dict(source_trace.output_json or {}),
            evidence_id_map,
        )
        carried_output = {
            **carried_output,
            "accepted": True,
            "_attempt_lineage": lineage,
        }
        if existing_trace is not None:
            existing_trace.tool_name = source_trace.tool_name
            existing_trace.input_json = carried_input
            existing_trace.output_json = carried_output
            existing_trace.created_at = source_trace.created_at
        else:
            db.add(
                V2ToolTrace(
                    generation_run_id=run.id,
                    corpus_revision=scope.revision,
                    attempt=scope.attempt,
                    phase="v2_source_triage",
                    agent_name="triage_attempt_lineage",
                    tool_name=source_trace.tool_name,
                    input_json=carried_input,
                    output_json=carried_output,
                    provider_call_id=marker,
                    created_at=source_trace.created_at,
                )
            )
        carried_count += 1
    await db.flush()
    return carried_count


def _source_map_action_evidence_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if key == "evidence_id":
                if str(item or "").strip():
                    result.append(str(item))
            elif key == "evidence_ids" and isinstance(item, list):
                result.extend(str(value) for value in item if str(value).strip())
            else:
                result.extend(_source_map_action_evidence_ids(item))
        return list(dict.fromkeys(result))
    if isinstance(value, list):
        return list(
            dict.fromkeys(
                evidence_id
                for item in value
                for evidence_id in _source_map_action_evidence_ids(item)
            )
        )
    return []


def _remap_source_map_action_evidence(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        remapped: dict[str, Any] = {}
        for key, item in value.items():
            if key == "evidence_id" and str(item or "").strip():
                remapped[key] = mapping.get(str(item), str(item))
            elif key == "evidence_ids" and isinstance(item, list):
                remapped[key] = list(
                    dict.fromkeys(mapping.get(str(evidence_id), str(evidence_id)) for evidence_id in item)
                )
            else:
                remapped[key] = _remap_source_map_action_evidence(item, mapping)
        return remapped
    if isinstance(value, list):
        return [_remap_source_map_action_evidence(item, mapping) for item in value]
    return value


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
    from app.generation_v2.source_triage_workspace_tools import build_workspace_tools as _build_workspace_tools

    return _build_workspace_tools(
        db=db,
        run=run,
        workspace=workspace,
        inventory=inventory,
        evidence_toolset=evidence_toolset,
        mode=mode,
        current_set_id=current_set_id,
    )


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
    source_set.setdefault("started_at", _now())
    source_set["updated_at"] = _now()
    _replace_workspace_set(current, source_set)
    current["current_set"] = {
        "set_id": set_id,
        "started_at": source_set["started_at"],
        "source_refs": source_set.get("source_refs") or [],
    }
    await save_source_triage_workspace(db, generation_run_id=run.id, workspace=current)
    return current


__all__ = [
    "build_workspace_tools",
    "create_repair_sets_from_gate",
    "ensure_capsules_for_refs",
    "ensure_source_triage_workspace",
    "_capsule_page_payload",
    "_dedupe",
    "load_source_triage_workspace",
    "mark_workspace_write_action",
    "pending_exact_read_source_refs",
    "planner_source_catalog",
    "record_triage_exact_read",
    "_merge_deferred",
    "_metadata_only_capsule",
    "_now",
    "_replace_workspace_set",
    "save_source_triage_workspace",
    "source_set_prompt_payload",
    "start_triage_source_set",
    "_stable_set_id",
    "triage_workflow_guard_error",
    "unprocessed_source_sets",
    "unresolved_source_set_summary",
    "unresolved_source_refs",
    "unresolved_source_sets",
    "_workspace_set",
    "_source_set_covered_refs",
    "_source_set_has_unresolved_refs",
    "workspace_summary_for_prompt",
    "clear_pending_exact_read",
]
