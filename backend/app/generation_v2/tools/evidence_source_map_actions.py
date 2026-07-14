from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import (
    QuoteCard,
    SourceCard,
    SourceCluster,
    SourceMap,
    SourceRelationship,
    SourceTriageItem,
    SourceTriageReport,
)
from app.generation_v2.corpus_scope import scope_for_run_view
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.generation_v2.tools.evidence_source_map_action_helpers import (
    cluster_id as _cluster_id,
    relationship_kind as _relationship_kind,
    selected_evidence_ids_from_actions as _selected_evidence_ids_from_actions,
    selected_role as _selected_role,
    trace_accepted as _trace_accepted,
)
from app.generation_v2.tools.evidence_types import _WRITE_TOOL_NAMES
from app.generation_v2.tools.evidence_utils import _confidence, _dedupe
from app.models.generation_v2 import EvidenceItem, V2ToolTrace


async def build_source_map_from_write_actions(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    inventory: SourceMap,
    run_view: Any | None = None,
) -> SourceMap:
    scope = scope_for_run_view(run_view)
    trace_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.corpus_revision == scope.revision,
            V2ToolTrace.attempt == scope.attempt,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_WRITE_TOOL_NAMES)),
        )
        .order_by(V2ToolTrace.created_at.asc())
    )
    traces = [trace for trace in trace_result.scalars() if _trace_accepted(trace)]
    evidence_result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == generation_run_id,
            EvidenceItem.corpus_revision == scope.revision,
            EvidenceItem.attempt == scope.attempt,
        )
    )
    evidence_by_id = {str(item.id): item for item in evidence_result.scalars()}
    inventory_by_source = {card.source_id: card for card in inventory.source_cards}
    source_ref_context = build_source_ref_context(inventory)
    selected_evidence_ids = _selected_evidence_ids_from_actions(traces, evidence_by_id)
    selected_evidence_by_id = {
        evidence_id: evidence_by_id[evidence_id]
        for evidence_id in selected_evidence_ids
        if evidence_id in evidence_by_id
    }

    source_cards_by_id: dict[str, SourceCard] = {}
    quote_cards: list[QuoteCard] = []
    clusters: list[SourceCluster] = []
    relationships: list[SourceRelationship] = []
    gaps: list[str] = []
    contradictions: list[str] = []
    retrieval_notes: list[str] = []
    finalized = False

    for trace in traces:
        action = trace.input_json or {}
        if trace.tool_name == "upsert_source_card":
            source_id = _action_source_id(action, source_ref_context, "source_ref", "source_id")
            base = inventory_by_source.get(source_id)
            if base is None:
                continue
            evidence_ids = _dedupe([str(eid) for eid in action.get("evidence_ids") or [] if str(eid) in selected_evidence_by_id])
            card = base.model_copy(deep=True)
            card.summary = str(action.get("summary") or base.summary)
            card.useful_for = [str(item) for item in action.get("useful_for") or [] if item]
            card.evidence_ids = evidence_ids
            card.gaps_or_cautions = [str(item) for item in action.get("gaps_or_cautions") or [] if item]
            card.coverage_status = "ready" if evidence_ids else card.coverage_status
            source_cards_by_id[source_id] = card
        elif trace.tool_name == "create_quote_card":
            evidence_id = str(action.get("evidence_id") or "")
            if evidence_id not in selected_evidence_by_id:
                continue
            evidence = selected_evidence_by_id[evidence_id]
            quote_cards.append(
                QuoteCard(
                    source_id=str(action.get("source_id") or evidence.source_id),
                    source_version_id=str(action.get("source_version_id") or evidence.source_version_id),
                    evidence_id=evidence_id,
                    quote=str(action.get("quote") or evidence.snippet_text),
                    locator=str(action.get("locator") or evidence.locator_text or ""),
                    narrative_use=str(action.get("narrative_use") or "triage"),
                    representation_id=str(evidence.source_version_id),
                    source_kind=evidence.retrieval_channel,
                    confidence=_confidence(action.get("confidence")),
                    confidence_reason=str(action.get("confidence_reason") or "Created from selected evidence."),
                )
            )
        elif trace.tool_name == "record_source_relationship":
            source_id = _action_source_id(action, source_ref_context, "source_ref", "source_id")
            related_source_id = _action_source_id(action, source_ref_context, "related_source_ref", "related_source_id")
            relationships.append(
                SourceRelationship(
                    source_id=source_id,
                    related_source_id=related_source_id,
                    relationship=_relationship_kind(str(action.get("relationship") or "")),
                    evidence_ids=[str(eid) for eid in action.get("evidence_ids") or [] if str(eid) in selected_evidence_by_id],
                    rationale=str(action.get("rationale") or ""),
                )
            )
        elif trace.tool_name == "record_source_cluster":
            cluster_source_ids = [
                _action_source_id({"source_ref": ref}, source_ref_context, "source_ref", "source_id")
                for ref in action.get("source_refs") or action.get("source_ids") or []
            ]
            primary_source_ids = [
                _action_source_id({"source_ref": ref}, source_ref_context, "source_ref", "source_id")
                for ref in action.get("primary_source_refs") or []
            ]
            secondary_source_ids = [
                _action_source_id({"source_ref": ref}, source_ref_context, "source_ref", "source_id")
                for ref in action.get("secondary_source_refs") or []
            ]
            duplicate_source_ids = [
                _action_source_id({"source_ref": ref}, source_ref_context, "source_ref", "source_id")
                for ref in action.get("duplicate_source_refs") or []
            ]
            clusters.append(
                SourceCluster(
                    cluster_id=_cluster_id(str(action.get("label") or ""), len(clusters) + 1),
                    label=str(action.get("label") or "Source cluster"),
                    source_ids=_dedupe([source_id for source_id in cluster_source_ids if source_id in inventory_by_source]),
                    primary_source_ids=_dedupe([source_id for source_id in primary_source_ids if source_id in inventory_by_source]),
                    secondary_source_ids=_dedupe([source_id for source_id in secondary_source_ids if source_id in inventory_by_source]),
                    duplicate_source_ids=_dedupe([source_id for source_id in duplicate_source_ids if source_id in inventory_by_source]),
                    evidence_ids=[
                        str(eid)
                        for eid in action.get("evidence_ids") or []
                        if str(eid) in selected_evidence_by_id
                    ],
                    rationale=str(action.get("rationale") or ""),
                )
            )
        elif trace.tool_name == "record_gap":
            description = str(action.get("description") or "")
            if not description:
                continue
            if str(action.get("kind") or "") == "contradiction":
                contradictions.append(description)
            else:
                gaps.append(description)
        elif trace.tool_name == "finalize_source_map":
            finalized = bool(action.get("ready"))
            retrieval_notes.append(str(action.get("summary") or "Source map finalized."))

    for evidence_id, evidence in selected_evidence_by_id.items():
        source_id = str(evidence.source_id)
        card = source_cards_by_id.get(source_id)
        if card is None:
            continue
        if evidence_id not in card.evidence_ids:
            card.evidence_ids.append(evidence_id)
        if not card.useful_for:
            card.useful_for = [_selected_role(evidence)]
        if card.coverage_status != "failed":
            card.coverage_status = "ready"
        source_cards_by_id[source_id] = card

    ranked = []
    for idx, card in enumerate(
        sorted(source_cards_by_id.values(), key=lambda item: (-len(item.evidence_ids), item.title)),
        start=1,
    ):
        ranked.append(
            SourceTriageItem(
                source_id=card.source_id,
                source_version_ids=card.source_version_ids,
                evidence_ids=card.evidence_ids,
                rank=idx,
                score=float(max(0, len(card.evidence_ids))),
                authority=card.authority,
                reliability=card.reliability,
                useful_for=card.useful_for,
                top_quote_refs=[quote.evidence_id or "" for quote in quote_cards if quote.source_id == card.source_id],
                relevance_reasons=["Selected by source triage write actions."],
                cautions=card.gaps_or_cautions,
            )
        )
    top_sources_by_function: dict[str, list[str]] = {}
    top_evidence_by_function: dict[str, list[str]] = {}
    for item in ranked:
        for function in item.useful_for or ["background"]:
            top_sources_by_function.setdefault(function, []).append(item.source_id)
            top_evidence_by_function.setdefault(function, []).extend(item.evidence_ids)
    if not finalized:
        gaps.append("Triage did not call finalize_source_map.")

    return SourceMap(
        source_cards=list(source_cards_by_id.values()),
        quote_cards=quote_cards,
        source_triage=SourceTriageReport(
            ranked_sources=ranked,
            top_sources_by_function={key: _dedupe(value) for key, value in top_sources_by_function.items()},
            top_evidence_by_function={key: _dedupe(value) for key, value in top_evidence_by_function.items()},
            gaps=gaps,
            contradictions=contradictions,
            source_clusters=clusters,
            source_relationships=relationships,
        ),
        evidence_ids=list(selected_evidence_by_id.keys()),
        gaps=gaps,
        contradictions=contradictions,
        retrieval_notes=retrieval_notes,
    )


def _action_source_id(action: dict[str, Any], context: Any, ref_key: str, id_key: str) -> str:
    value = str(action.get(ref_key) or action.get(id_key) or "")
    return resolve_source_ref(value, context) or ""


__all__ = [
    "build_source_map_from_write_actions",
    "_relationship_kind",
]
