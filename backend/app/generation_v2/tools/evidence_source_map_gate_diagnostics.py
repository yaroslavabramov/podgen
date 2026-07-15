from __future__ import annotations

from app.generation_v2.schemas import SourceCard, SourceMap, SourceTriageCompletion
from app.generation_v2.tools.evidence_core_utils import evidence_origin_retrieval_channel
from app.generation_v2.tools.evidence_utils import _dedupe
from app.models.generation_v2 import EvidenceItem, EvidenceRetrievalChannel, EvidenceStrength


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


def missing_source_card_evidence_sources(
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


def missing_model_read_sources(
    source_map: SourceMap,
    evidence_by_id: dict[str, EvidenceItem],
    selected_evidence_ids: set[str],
) -> list[str]:
    missing: list[str] = []
    for card in source_map.source_cards:
        if not source_card_requires_direct_read_diagnostic(card):
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
            and evidence_origin_retrieval_channel(evidence_by_id[evidence_id])
            == EvidenceRetrievalChannel.MODEL_FILE_READ
            and str(evidence_by_id[evidence_id].source_id) == card.source_id
            and evidence_by_id[evidence_id].evidence_strength in {EvidenceStrength.EXACT, EvidenceStrength.SNIPPET}
            for evidence_id in model_read_candidate_ids
        )
        if not has_model_read:
            missing.append(card.source_id)
    return sorted(_dedupe(missing))


def unpersisted_completion_gap_notes(
    triage_completion: SourceTriageCompletion | None,
    source_map: SourceMap,
) -> list[str]:
    if triage_completion is None:
        return []
    notes = [str(note).strip() for note in triage_completion.notes if str(note).strip()]
    gap_like_notes = [note for note in notes if looks_like_gap_note(note)]
    if not gap_like_notes:
        return []
    persisted_gaps = [
        str(gap).strip() for gap in [*source_map.gaps, *source_map.source_triage.gaps] if str(gap).strip()
    ]
    if persisted_gaps:
        return []
    return gap_like_notes


def looks_like_gap_note(note: str) -> bool:
    lowered = note.lower()
    return any(marker in lowered for marker in _GAP_NOTE_MARKERS)


def source_card_requires_direct_read_diagnostic(card: SourceCard) -> bool:
    source_type = str(card.source_type or "").lower()
    if source_type == "pdf":
        return True
    summary = f"{card.title} {card.url or ''} {card.representation_summary}".lower()
    return ".pdf" in summary or "original_file" in summary
