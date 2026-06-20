from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.schemas import (
    QuoteCard,
    SourceCard,
    SourceMap,
    SourceRelationship,
    SourceTriageCompletion,
    SourceTriageGate,
    SourceTriageItem,
    SourceTriageReport,
)
from app.ingestion.source_metadata import audited_authority_type
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
    RunCorpusView,
    SourceVersion,
    V2RecordStatus,
    V2ToolTrace,
)
from app.models.source import Source
from app.generation_v2.tools.evidence_types import (
    EVIDENCE_STATUS_CITED,
    EVIDENCE_STATUS_SELECTED,
    _TRIAGE_READING_TOOL_NAMES,
    _WRITE_TOOL_NAMES,
)
from app.generation_v2.tools.evidence_utils import (
    _confidence,
    _dedupe,
    _inventory_cautions,
    _neutral_preview,
    _provider_status,
    _readable_version_priority,
    _source_domain,
    _source_length_hint,
    _uuid_or_none,
    _version_readable,
)


async def build_source_inventory(db: AsyncSession, run_view: RunCorpusView) -> SourceMap:
    """Build neutral, deterministic source inventory for agent-led triage."""
    selected_source_ids = [uuid.UUID(source_id) for source_id in run_view.selected_source_ids if _uuid_or_none(source_id)]
    selected_version_ids = [uuid.UUID(version_id) for version_id in run_view.selected_source_version_ids if _uuid_or_none(version_id)]
    if not selected_source_ids:
        return SourceMap(gaps=["Run corpus view has no selected sources."])
    source_result = await db.execute(select(Source).where(Source.id.in_(selected_source_ids)))
    sources_by_id = {str(source.id): source for source in source_result.scalars()}
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.id.in_(selected_version_ids)))
    versions_by_source: dict[str, list[SourceVersion]] = {}
    for version in version_result.scalars():
        versions_by_source.setdefault(str(version.source_id), []).append(version)
    provider_result = await db.execute(select(ProviderCorpusFile).where(ProviderCorpusFile.source_version_id.in_(selected_version_ids)))
    provider_by_version = {str(item.source_version_id): item for item in provider_result.scalars()}

    cards: list[SourceCard] = []
    for source_id in run_view.selected_source_ids:
        source = sources_by_id.get(str(source_id))
        if source is None:
            continue
        versions = sorted(versions_by_source.get(str(source.id), []), key=lambda version: _readable_version_priority(version.kind))
        provider_statuses = [
            provider_by_version[str(version.id)].status
            for version in versions
            if str(version.id) in provider_by_version
        ]
        preview = _neutral_preview(source, versions)
        cards.append(
            SourceCard(
                source_id=str(source.id),
                source_version_ids=[str(version.id) for version in versions],
                evidence_ids=[],
                title=source.title or source.original_url or source.file_path or "Untitled source",
                source_type=source.source_type,
                url=source.original_url,
                domain=_source_domain(source.original_url),
                published_at=source.published_at.isoformat() if source.published_at else None,
                length_hint=_source_length_hint(source, versions),
                provider_status=_provider_status(provider_statuses),
                reliability=source.domain_reliability or "unknown",
                authority=audited_authority_type(source),
                summary=preview,
                useful_for=[],
                gaps_or_cautions=_inventory_cautions(source, versions, provider_statuses),
                representation_ids=[str(version.id) for version in versions],
                representation_summary=", ".join(
                    f"{version.kind}:{version.status}/{version.upload_status}" for version in versions[:8]
                ),
                ready_representation_count=sum(1 for version in versions if version.status == V2RecordStatus.READY),
                failed_representation_count=sum(1 for version in versions if version.status == V2RecordStatus.FAILED),
                coverage_status="ready" if any(_version_readable(version) for version in versions) else "partial",
            )
        )
    return SourceMap(
        source_cards=cards,
        gaps=[] if cards else ["No selected source could be loaded into inventory."],
        retrieval_notes=["Deterministic neutral inventory only; no editorial source-map search has been applied."],
    )


async def build_source_map_from_write_actions(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    inventory: SourceMap,
) -> SourceMap:
    trace_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_WRITE_TOOL_NAMES)),
        )
        .order_by(V2ToolTrace.created_at.asc())
    )
    traces = [trace for trace in trace_result.scalars() if _trace_accepted(trace)]
    evidence_result = await db.execute(select(EvidenceItem).where(EvidenceItem.generation_run_id == generation_run_id))
    evidence_by_id = {str(item.id): item for item in evidence_result.scalars()}
    inventory_by_source = {card.source_id: card for card in inventory.source_cards}
    selected_evidence_ids = _selected_evidence_ids_from_actions(traces, evidence_by_id)
    selected_evidence_by_id = {
        evidence_id: evidence_by_id[evidence_id]
        for evidence_id in selected_evidence_ids
        if evidence_id in evidence_by_id
    }

    source_cards_by_id: dict[str, SourceCard] = {}
    quote_cards: list[QuoteCard] = []
    relationships: list[SourceRelationship] = []
    gaps: list[str] = []
    contradictions: list[str] = []
    retrieval_notes: list[str] = []
    finalized = False

    for trace in traces:
        action = trace.input_json or {}
        if trace.tool_name == "upsert_source_card":
            source_id = str(action.get("source_id") or "")
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
                    quote=str(action.get("quote") or evidence.snippet_text[:500]),
                    locator=str(action.get("locator") or evidence.locator_text or ""),
                    narrative_use=str(action.get("narrative_use") or "triage"),
                    representation_id=str(evidence.source_version_id),
                    source_kind=evidence.retrieval_channel,
                    confidence=_confidence(action.get("confidence")),
                    confidence_reason=str(action.get("confidence_reason") or "Created from selected evidence."),
                )
            )
        elif trace.tool_name == "record_source_relationship":
            relationships.append(
                SourceRelationship(
                    source_id=str(action.get("source_id") or ""),
                    related_source_id=str(action.get("related_source_id") or ""),
                    relationship=_relationship_kind(str(action.get("relationship") or "")),
                    evidence_ids=[str(eid) for eid in action.get("evidence_ids") or [] if str(eid) in selected_evidence_by_id],
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
        base = inventory_by_source.get(source_id)
        if base is None:
            continue
        card = source_cards_by_id.get(source_id) or base.model_copy(deep=True)
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
                top_quote_refs=[quote.evidence_id or "" for quote in quote_cards if quote.source_id == card.source_id][:6],
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
            top_sources_by_function={key: _dedupe(value)[:10] for key, value in top_sources_by_function.items()},
            top_evidence_by_function={key: _dedupe(value)[:12] for key, value in top_evidence_by_function.items()},
            gaps=gaps,
            contradictions=contradictions,
            source_relationships=relationships,
        ),
        evidence_ids=list(selected_evidence_by_id.keys()),
        gaps=gaps,
        contradictions=contradictions,
        retrieval_notes=retrieval_notes,
    )


async def validate_source_map_gate(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    source_map: SourceMap,
    run_view: RunCorpusView,
    triage_completion: SourceTriageCompletion | None = None,
) -> SourceTriageGate:
    selected_source_ids = {str(item) for item in run_view.selected_source_ids}
    selected_version_ids = {str(item) for item in run_view.selected_source_version_ids}
    evidence_result = await db.execute(select(EvidenceItem).where(EvidenceItem.generation_run_id == generation_run_id))
    evidence_by_id = {str(item.id): item for item in evidence_result.scalars()}
    selected_evidence_ids = {
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if (evidence.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
    }
    trace_result = await db.execute(
        select(V2ToolTrace).where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_TRIAGE_READING_TOOL_NAMES)),
        )
    )
    triage_traces = list(trace_result.scalars())
    tool_call_count = len(triage_traces)
    referenced_source_ids = _source_map_source_ids(source_map)
    referenced_version_ids = _source_map_version_ids(source_map)
    referenced_evidence_ids = set(_source_map_evidence_ids(source_map))
    invalid_source_ids = sorted(source_id for source_id in referenced_source_ids if source_id not in selected_source_ids)
    invalid_version_ids = sorted(version_id for version_id in referenced_version_ids if version_id not in selected_version_ids)
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
        valid_relation_evidence = [eid for eid in relation.evidence_ids if eid in selected_evidence_ids]
        if relation.relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"} and not valid_relation_evidence:
            ungrounded_relationships.append(f"{relation.source_id}:{relation.relationship}:{relation.related_source_id}")
    for cluster in source_map.source_triage.source_clusters:
        if (cluster.primary_source_ids or cluster.secondary_source_ids or cluster.duplicate_source_ids) and not [
            eid for eid in cluster.evidence_ids if eid in selected_evidence_ids
        ]:
            ungrounded_relationships.append(f"cluster:{cluster.cluster_id}")
    missing_function_evidence = [
        function
        for function, evidence_ids in source_map.source_triage.top_evidence_by_function.items()
        if not [eid for eid in evidence_ids if eid in selected_evidence_ids and eid not in invalid_evidence_ids]
    ]
    if not source_map.source_triage.top_evidence_by_function:
        missing_function_evidence.append("top_evidence_by_function")
    warnings = []
    if not source_map.quote_cards:
        warnings.append("Triage produced no quote cards.")
    if not source_map.retrieval_notes:
        warnings.append("Triage produced no retrieval notes.")
    requires_tool_call = len(selected_source_ids) > 1
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
    missing_model_read_source_ids = _missing_model_read_sources(source_map, evidence_by_id, selected_evidence_ids)
    if missing_model_read_source_ids:
        warnings.append("Some PDF/document source cards lack direct model_file_read evidence.")
    if missing_function_evidence:
        warnings.append("Some triage function seed evidence is missing or incomplete.")
    unpersisted_completion_gap_notes = _unpersisted_completion_gap_notes(triage_completion, source_map)
    missing_source_card_write_source_ids = _missing_source_card_write_sources(source_map, triage_traces)
    passed = not (
        (requires_tool_call and tool_call_count == 0)
        or "Triage did not call finalize_source_map." in source_map.gaps
        or evidence_count == 0
        or invalid_source_ids
        or invalid_version_ids
        or invalid_evidence_ids
        or missing_source_card_evidence_source_ids
        or missing_source_card_write_source_ids
        or unpersisted_completion_gap_notes
        or ungrounded_relationships
    )
    summary = "Source triage gate passed." if passed else "Source triage gate failed."
    if requires_tool_call and tool_call_count == 0:
        summary += " No triage reading/search tool calls were recorded."
    if "Triage did not call finalize_source_map." in source_map.gaps:
        summary += " Source triage did not finalize the source map."
    if evidence_count == 0:
        summary += " No valid persisted evidence ids were referenced."
    if missing_source_card_evidence_source_ids:
        summary += " Every SourceCard must contain at least one selected evidence id."
    if missing_model_read_source_ids:
        summary += " Some PDF/document SourceCards lack selected direct model_file_read evidence; treating as a coverage warning."
    if missing_source_card_write_source_ids:
        summary += " Source cards must come from accepted upsert_source_card write actions."
    if unpersisted_completion_gap_notes:
        summary += " Triage completion mentions gaps/cautions that were not persisted with record_gap."
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
    )


async def _source_summaries(db: AsyncSession, run_view: RunCorpusView, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources_result = await db.execute(select(Source).where(Source.id.in_([uuid.UUID(source_id) for source_id in run_view.selected_source_ids])))
    evidence_by_source: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        evidence_by_source.setdefault(str(item.get("source_id")), []).append(item)
    return [
        {
            "source_id": str(source.id),
            "title": source.title or source.original_url or source.file_path or "Untitled source",
            "source_type": source.source_type,
            "authority": source.authority_type,
            "reliability": source.domain_reliability,
            "evidence_ids": [
                str(item.get("evidence_id") or item.get("id") or "")
                for item in evidence_by_source.get(str(source.id), [])[:6]
                if item.get("evidence_id") or item.get("id")
            ],
        }
        for source in sources_result.scalars()
    ]


def _source_map_evidence_ids(source_map: SourceMap) -> list[str]:
    ids = [*source_map.evidence_ids]
    ids.extend(eid for card in source_map.source_cards for eid in card.evidence_ids)
    ids.extend(quote.evidence_id for quote in source_map.quote_cards if quote.evidence_id)
    ids.extend(eid for item in source_map.source_triage.ranked_sources for eid in item.evidence_ids)
    ids.extend(eid for values in source_map.source_triage.top_evidence_by_function.values() for eid in values)
    ids.extend(eid for cluster in source_map.source_triage.source_clusters for eid in cluster.evidence_ids)
    ids.extend(eid for relation in source_map.source_triage.source_relationships for eid in relation.evidence_ids)
    return _dedupe([str(item) for item in ids if item])


def _trace_accepted(trace: V2ToolTrace) -> bool:
    output = getattr(trace, "output_json", None) or {}
    return bool(output.get("accepted", True))


def _selected_evidence_ids_from_actions(traces: list[V2ToolTrace], evidence_by_id: dict[str, EvidenceItem]) -> list[str]:
    selected = [
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if (evidence.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
    ]
    for trace in traces:
        if trace.tool_name != "select_evidence":
            continue
        evidence_id = str((trace.input_json or {}).get("evidence_id") or "")
        if evidence_id:
            selected.append(evidence_id)
    return _dedupe(selected)


def _selected_role(evidence: EvidenceItem) -> str:
    metadata = evidence.metadata_ or {}
    return str(metadata.get("selected_role") or metadata.get("selected_purpose") or evidence.purpose or "background")


def _source_map_source_ids(source_map: SourceMap) -> set[str]:
    ids = {card.source_id for card in source_map.source_cards}
    ids.update(quote.source_id for quote in source_map.quote_cards)
    ids.update(item.source_id for item in source_map.source_triage.ranked_sources)
    ids.update(source_id for values in source_map.source_triage.top_sources_by_function.values() for source_id in values)
    for cluster in source_map.source_triage.source_clusters:
        ids.update(cluster.source_ids)
        ids.update(cluster.primary_source_ids)
        ids.update(cluster.secondary_source_ids)
        ids.update(cluster.duplicate_source_ids)
    for relation in source_map.source_triage.source_relationships:
        ids.add(relation.source_id)
        ids.add(relation.related_source_id)
    return {str(item) for item in ids if item}


def _source_map_version_ids(source_map: SourceMap) -> set[str]:
    ids = {version_id for card in source_map.source_cards for version_id in card.source_version_ids}
    ids.update(quote.source_version_id for quote in source_map.quote_cards if quote.source_version_id)
    ids.update(version_id for item in source_map.source_triage.ranked_sources for version_id in item.source_version_ids)
    return {str(item) for item in ids if item}


def _missing_source_card_evidence_sources(
    source_map: SourceMap,
    evidence_by_id: dict[str, EvidenceItem],
    selected_evidence_ids: set[str],
    *,
    invalid_evidence_ids: set[str],
) -> list[str]:
    missing = []
    for card in source_map.source_cards:
        valid_card_evidence = [
            evidence_id
            for evidence_id in card.evidence_ids
            if evidence_id in evidence_by_id
            and evidence_id in selected_evidence_ids
            and evidence_id not in invalid_evidence_ids
            and str(evidence_by_id[evidence_id].source_id) == card.source_id
        ]
        if not valid_card_evidence:
            missing.append(card.source_id)
    return sorted(_dedupe(missing))


def _missing_model_read_sources(
    source_map: SourceMap,
    evidence_by_id: dict[str, EvidenceItem],
    selected_evidence_ids: set[str],
) -> list[str]:
    missing: list[str] = []
    for card in source_map.source_cards:
        if not _source_card_needs_model_read(card):
            continue
        source_top_evidence_ids = {
            evidence_id
            for values in source_map.source_triage.top_evidence_by_function.values()
            for evidence_id in values
            if evidence_id in evidence_by_id and str(evidence_by_id[evidence_id].source_id) == card.source_id
        }
        model_read_candidate_ids = set(card.evidence_ids).union(source_top_evidence_ids)
        candidate_evidence = [
            evidence_by_id[evidence_id]
            for evidence_id in model_read_candidate_ids
            if evidence_id in evidence_by_id and evidence_id in selected_evidence_ids
        ]
        if not candidate_evidence:
            missing.append(card.source_id)
            continue
        has_model_read = any(
            evidence_id in evidence_by_id
            and evidence_id in selected_evidence_ids
            and getattr(evidence_by_id[evidence_id], "retrieval_channel", None) == EvidenceRetrievalChannel.MODEL_FILE_READ
            and str(evidence_by_id[evidence_id].source_id) == card.source_id
            and evidence_by_id[evidence_id].evidence_strength in {EvidenceStrength.EXACT, EvidenceStrength.SNIPPET}
            for evidence_id in model_read_candidate_ids
        )
        if not has_model_read:
            missing.append(card.source_id)
    return sorted(_dedupe(missing))


def _missing_source_card_write_sources(source_map: SourceMap, traces: list[V2ToolTrace]) -> list[str]:
    upserted = {
        str((getattr(trace, "input_json", None) or {}).get("source_id") or "")
        for trace in traces
        if getattr(trace, "tool_name", "") == "upsert_source_card" and _trace_accepted(trace)
    }
    return sorted(card.source_id for card in source_map.source_cards if card.source_id not in upserted)


_GAP_NOTE_MARKERS = (
    "gap",
    "gaps",
    "lacuna",
    "missing",
    "not captured",
    "not observed",
    "lack",
    "lacks",
    "needs primary",
    "requires primary",
    "requires additional",
    "should add",
    "needs direct",
    "лакун",
    "пробел",
    "не хватает",
    "нет ",
    "не найден",
    "не захвачен",
    "не подтянут",
    "требует",
    "требуется",
    "желательно добавить",
    "нужно добавить",
)


def _unpersisted_completion_gap_notes(
    triage_completion: SourceTriageCompletion | None,
    source_map: SourceMap,
) -> list[str]:
    if triage_completion is None:
        return []
    notes = [str(note).strip() for note in triage_completion.notes if str(note).strip()]
    gap_like_notes = [note for note in notes if _looks_like_gap_note(note)]
    if not gap_like_notes:
        return []
    persisted_gaps = [str(gap).strip() for gap in [*source_map.gaps, *source_map.source_triage.gaps] if str(gap).strip()]
    if persisted_gaps:
        return []
    return gap_like_notes


def _looks_like_gap_note(note: str) -> bool:
    lowered = note.lower()
    return any(marker in lowered for marker in _GAP_NOTE_MARKERS)


def _source_card_needs_model_read(card: SourceCard) -> bool:
    source_type = str(card.source_type or "").lower()
    if source_type == "pdf":
        return True
    summary = f"{card.title} {card.url or ''} {card.representation_summary}".lower()
    return ".pdf" in summary or "original_file" in summary


def _relationship_kind(value: str) -> str:
    allowed = {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates", "background_for"}
    return value if value in allowed else "background_for"


__all__ = [name for name in globals() if not name.startswith('__')]
