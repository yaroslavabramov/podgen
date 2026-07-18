from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import SourceMap, SourceTriageCompletion, SourceTriageGate
from app.generation_v2.corpus_scope import scope_for_run_view
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.generation_v2.tools.evidence_source_inventory import build_source_inventory
from app.generation_v2.tools.evidence_source_map_gate_diagnostics import (
    missing_model_read_sources as _missing_model_read_sources,
    missing_source_card_evidence_sources as _missing_source_card_evidence_sources,
    unpersisted_completion_gap_notes as _unpersisted_completion_gap_notes,
)
from app.generation_v2.tools.evidence_source_map_ids import (
    source_map_evidence_ids as _source_map_evidence_ids,
    source_map_source_ids as _source_map_source_ids,
    source_map_version_ids as _source_map_version_ids,
)
from app.generation_v2.tools.evidence_types import (
    EVIDENCE_STATUS_CITED,
    EVIDENCE_STATUS_SELECTED,
    _TRIAGE_READING_TOOL_NAMES,
)
from app.models.generation_v2 import EvidenceItem, RunCorpusView, V2ToolTrace
from app.generation_v2.source_triage_canonical import canonical_workspace_violations


async def validate_source_map_gate(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    source_map: SourceMap,
    run_view: RunCorpusView,
    triage_completion: SourceTriageCompletion | None = None,
    triage_workspace: dict[str, Any] | None = None,
) -> SourceTriageGate:
    from app.generation_v2.source_triage_workspace_state import (
        is_structured_triage_workspace,
        unresolved_source_refs,
    )

    scope = scope_for_run_view(run_view)
    is_v2_workspace = is_structured_triage_workspace(triage_workspace)
    workspace_violations = (
        canonical_workspace_violations(triage_workspace or {}, require_syntheses=True)
        if is_v2_workspace
        else []
    )
    selected_source_ids = {str(item) for item in run_view.selected_source_ids}
    selected_version_ids = {str(item) for item in run_view.selected_source_version_ids}
    evidence_result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == generation_run_id,
            EvidenceItem.corpus_revision == scope.revision,
            EvidenceItem.attempt == scope.attempt,
        )
    )
    evidence_by_id = {str(item.id): item for item in evidence_result.scalars()}
    selected_evidence_ids = {
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if (evidence.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
    }
    trace_result = await db.execute(
        select(V2ToolTrace).where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.corpus_revision == scope.revision,
            V2ToolTrace.attempt == scope.attempt,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_TRIAGE_READING_TOOL_NAMES)),
        )
    )
    triage_traces = list(trace_result.scalars())
    tool_call_count = len(triage_traces)
    referenced_source_ids = _source_map_source_ids(source_map)
    referenced_version_ids = _source_map_version_ids(source_map)
    referenced_evidence_ids = set(_source_map_evidence_ids(source_map))
    invalid_source_ids = sorted(
        source_id for source_id in referenced_source_ids if source_id not in selected_source_ids
    )
    invalid_version_ids = sorted(
        version_id for version_id in referenced_version_ids if version_id not in selected_version_ids
    )
    invalid_evidence_ids = sorted(
        evidence_id
        for evidence_id in referenced_evidence_ids
        if evidence_id not in evidence_by_id
        or evidence_id not in selected_evidence_ids
        or str(evidence_by_id[evidence_id].source_id) not in selected_source_ids
        or str(evidence_by_id[evidence_id].source_version_id) not in selected_version_ids
    )
    ungrounded_relationships = []
    for relation in source_map.source_triage.source_relationships:
        relation_source_ids = {relation.source_id, relation.related_source_id}
        if relation.source_id == relation.related_source_id:
            ungrounded_relationships.append(
                f"relationship:self:{relation.source_id}:{relation.relationship}"
            )
        invalid_relation_scope = [
            evidence_id
            for evidence_id in relation.evidence_ids
            if evidence_id in evidence_by_id
            and str(evidence_by_id[evidence_id].source_id) not in relation_source_ids
        ]
        if invalid_relation_scope:
            ungrounded_relationships.append(
                f"relationship:evidence_scope:{relation.source_id}:{relation.relationship}:{relation.related_source_id}"
            )
        valid_relation_evidence = [
            evidence_id
            for evidence_id in relation.evidence_ids
            if evidence_id in selected_evidence_ids
            and evidence_id in evidence_by_id
            and str(evidence_by_id[evidence_id].source_id) in relation_source_ids
        ]
        if (
            relation.relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"}
            and not valid_relation_evidence
        ):
            ungrounded_relationships.append(
                f"{relation.source_id}:{relation.relationship}:{relation.related_source_id}"
            )
    for cluster in source_map.source_triage.source_clusters:
        cluster_source_ids = set(cluster.source_ids)
        if len(cluster_source_ids) < 2:
            ungrounded_relationships.append(f"cluster:membership:{cluster.cluster_id}")
        role_sets = {
            "primary": set(cluster.primary_source_ids),
            "secondary": set(cluster.secondary_source_ids),
            "duplicate": set(cluster.duplicate_source_ids),
        }
        if any(role_source_ids - cluster_source_ids for role_source_ids in role_sets.values()):
            ungrounded_relationships.append(f"cluster:role_scope:{cluster.cluster_id}")
        if any(
            left_ids & right_ids
            for index, left_ids in enumerate(role_sets.values())
            for right_ids in list(role_sets.values())[index + 1 :]
        ):
            ungrounded_relationships.append(f"cluster:role_overlap:{cluster.cluster_id}")
        invalid_cluster_scope = [
            evidence_id
            for evidence_id in cluster.evidence_ids
            if evidence_id in evidence_by_id
            and str(evidence_by_id[evidence_id].source_id) not in cluster_source_ids
        ]
        if invalid_cluster_scope:
            ungrounded_relationships.append(f"cluster:evidence_scope:{cluster.cluster_id}")
        valid_cluster_evidence = [
            evidence_id
            for evidence_id in cluster.evidence_ids
            if evidence_id in selected_evidence_ids
            and evidence_id in evidence_by_id
            and str(evidence_by_id[evidence_id].source_id) in cluster_source_ids
        ]
        if (cluster.primary_source_ids or cluster.secondary_source_ids or cluster.duplicate_source_ids) and not valid_cluster_evidence:
            ungrounded_relationships.append(f"cluster:{cluster.cluster_id}")
    for gap in source_map.gap_records:
        gap_source_ids = set(gap.source_ids)
        if any(
            evidence_id in evidence_by_id
            and str(evidence_by_id[evidence_id].source_id) not in gap_source_ids
            for evidence_id in gap.evidence_ids
        ):
            ungrounded_relationships.append(f"gap:evidence_scope:{gap.gap_id}")
    ungrounded_relationships = sorted(set(ungrounded_relationships))
    missing_function_evidence = [
        function
        for function, evidence_ids in source_map.source_triage.top_evidence_by_function.items()
        if not [eid for eid in evidence_ids if eid in selected_evidence_ids and eid not in invalid_evidence_ids]
    ]
    if not source_map.source_triage.top_evidence_by_function:
        missing_function_evidence.append("top_evidence_by_function")
    warnings = []
    if not source_map.retrieval_notes:
        warnings.append("Triage produced no retrieval notes.")
    requires_tool_call = len(selected_source_ids) > 1 and not is_v2_workspace
    unselected_quote_evidence = [
        str(quote.evidence_id)
        for quote in source_map.quote_cards
        if quote.evidence_id and quote.evidence_id not in selected_evidence_ids
    ]
    if unselected_quote_evidence:
        invalid_evidence_ids = sorted({*invalid_evidence_ids, *unselected_quote_evidence})
    evidence_count = len([eid for eid in referenced_evidence_ids if eid in selected_evidence_ids])
    missing_source_card_evidence_source_ids = _missing_source_card_evidence_sources(
        source_map,
        evidence_by_id,
        selected_evidence_ids,
        invalid_evidence_ids=set(invalid_evidence_ids),
    )
    if is_v2_workspace:
        # V2 records source usability and unreadable/deferred sources explicitly.
        # A SourceCard is orientation, not a requirement to manufacture evidence.
        missing_source_card_evidence_source_ids = []
    missing_model_read_source_ids = _missing_model_read_sources(source_map, evidence_by_id, selected_evidence_ids)
    if missing_function_evidence:
        warnings.append("Some triage function seed evidence is missing or incomplete.")
    unpersisted_completion_gap_notes = _unpersisted_completion_gap_notes(triage_completion, source_map)
    if is_v2_workspace:
        unpersisted_completion_gap_notes = []
    try:
        write_source_ref_context = build_source_ref_context(await build_source_inventory(db, run_view))
    except Exception:
        write_source_ref_context = build_source_ref_context(source_map)
    missing_source_card_write_source_ids = missing_source_card_write_sources(
        source_map,
        triage_traces,
        write_source_ref_context,
    )
    if is_v2_workspace:
        missing_source_card_write_source_ids = []
    unresolved_refs = unresolved_source_refs(triage_workspace or {}) if triage_workspace is not None else []
    completion_not_ready = triage_completion is not None and not triage_completion.ready
    passed = not (
        (requires_tool_call and tool_call_count == 0)
        or "Triage did not call finalize_source_map." in source_map.gaps
        or (evidence_count == 0 and not is_v2_workspace)
        or invalid_source_ids
        or invalid_version_ids
        or invalid_evidence_ids
        or missing_source_card_evidence_source_ids
        or missing_source_card_write_source_ids
        or unpersisted_completion_gap_notes
        or ungrounded_relationships
        or unresolved_refs
        or workspace_violations
        or completion_not_ready
    )
    summary = "Source triage gate passed." if passed else "Source triage gate failed."
    if requires_tool_call and tool_call_count == 0:
        summary += " No triage reading/search tool calls were recorded."
    if "Triage did not call finalize_source_map." in source_map.gaps:
        summary += " Source triage did not finalize the source map."
    if evidence_count == 0 and not is_v2_workspace:
        summary += " No valid persisted evidence ids were referenced."
    if missing_source_card_evidence_source_ids:
        summary += " Every SourceCard must contain at least one selected evidence id."
    if missing_source_card_write_source_ids:
        summary += " Source cards must come from accepted upsert_source_card write actions."
    if unpersisted_completion_gap_notes:
        summary += " Triage completion mentions gaps/cautions that were not persisted with record_gap."
    if ungrounded_relationships:
        summary += " Source relationships, clusters, or gaps contain invalid structural grounding."
    if unresolved_refs:
        summary += " Every run source must be mapped or explicitly deferred."
    if workspace_violations:
        summary += " Canonical source assessments or coverage outcomes are incomplete."
    if completion_not_ready:
        summary += " Source triage workflow did not complete durably."
    return SourceTriageGate(
        passed=passed,
        summary=summary,
        tool_call_count=tool_call_count,
        checked_source_count=len(referenced_source_ids),
        checked_evidence_count=evidence_count,
        invalid_source_ids=invalid_source_ids,
        invalid_source_version_ids=invalid_version_ids,
        invalid_evidence_ids=invalid_evidence_ids,
        missing_source_card_evidence_source_ids=missing_source_card_evidence_source_ids,
        missing_model_read_source_ids=missing_model_read_source_ids,
        missing_source_card_write_source_ids=missing_source_card_write_source_ids,
        unpersisted_completion_gap_notes=unpersisted_completion_gap_notes,
        ungrounded_relationships=ungrounded_relationships,
        missing_function_evidence=missing_function_evidence,
        warnings=warnings,
        unresolved_source_refs=unresolved_refs,
        workspace_violations=workspace_violations,
    )


def missing_source_card_write_sources(
    source_map: SourceMap,
    traces: list[V2ToolTrace],
    source_ref_context: Any,
) -> list[str]:
    upserted = {
        _action_source_id(getattr(trace, "input_json", None) or {}, source_ref_context, "source_ref", "source_id")
        for trace in traces
        if getattr(trace, "tool_name", "") == "upsert_source_card" and _trace_accepted(trace)
    }
    return sorted(card.source_id for card in source_map.source_cards if card.source_id not in upserted)


def _action_source_id(action: dict[str, Any], context: Any, ref_key: str, id_key: str) -> str:
    value = str(action.get(ref_key) or action.get(id_key) or "")
    return resolve_source_ref(value, context) or ""


def _trace_accepted(trace: V2ToolTrace) -> bool:
    output = getattr(trace, "output_json", None) or {}
    return bool(output.get("accepted", True))
