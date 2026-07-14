from __future__ import annotations

from typing import Any

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
    source_set_prompt_payload,
    unprocessed_source_sets,
    unresolved_source_set_summary,
    unresolved_source_sets,
    workspace_summary_for_prompt,
)
from app.generation_v2.source_triage_workspace_repair import create_repair_sets_from_gate
from app.generation_v2.source_triage_repair_state import carry_pending_triage_repair
from app.generation_v2.source_triage_workspace_tracking import (
    mark_workspace_write_action,
    triage_workflow_guard_error,
)
from app.generation_v2.tools.evidence_refs import build_source_ref_context, source_ref_for_id
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import RunCorpusView


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
    workspace = {
        "schema_version": WORKSPACE_SCHEMA_VERSION,
        "manifest_hash": manifest_hash,
        "corpus_revision": scope.revision,
        "attempt": scope.attempt,
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
    workspace = carry_pending_triage_repair(existing or {}, workspace, source_order=source_order)
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
    source_set["started_at"] = _now()
    source_set["updated_at"] = _now()
    _replace_workspace_set(current, source_set)
    current["current_set"] = {"set_id": set_id, "started_at": source_set["started_at"], "source_refs": source_set.get("source_refs") or []}
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
]
