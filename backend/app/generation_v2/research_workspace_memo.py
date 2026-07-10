from __future__ import annotations

from app.generation_v2.contracts.research import ResearchGapSeverity, ResearchGapStatus, ResearchGapTreatmentRelevance
from app.generation_v2.research_workspace_helpers import (
    active_atom_drafts as _active_atom_drafts,
    coerce_atom_kind as _coerce_atom_kind,
    confidence as _confidence,
    coverage_status as _coverage_status,
    dedupe as _dedupe,
    dedupe_normalized as _dedupe_normalized,
    draft_is_source_quality_note as _draft_is_source_quality_note,
    source_quality_risk_from_draft as _source_quality_risk_from_draft,
    story_weight as _story_weight,
)
from app.generation_v2.research_workspace_gap_helpers import (
    dedupe_gaps as _dedupe_gaps,
    risk_from_gap as _risk_from_gap,
    treatment_relevance as _treatment_relevance,
)
from app.generation_v2.research_workspace_state import normalize_workspace as _normalize_workspace
from app.generation_v2.schemas import EditorialAtom, ResearchMemo
from app.models.generation_run import GenerationRun


def research_memo_from_workspace(run: GenerationRun, workspace: dict) -> ResearchMemo:
    workspace = _normalize_workspace(workspace)
    fields = dict(workspace.get("memo_fields") or {})
    title = fields.get("title") or str((run.config or {}).get("user_prompt") or "Podcast episode")
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
                title=str(draft.get("title") or "Editorial atom"),
                explanation=str(draft.get("explanation") or draft.get("claim") or ""),
                source_ids=_dedupe([str(item) for item in draft.get("source_ids") or [] if str(item).strip()]),
                quote_refs=[str(item) for item in draft.get("quote_refs") or [] if str(item).strip()],
                story_value=str(draft.get("story_value") or "Use in treatment."),
                anchor_refs=anchor_refs,
                evidence_ids=evidence_ids,
                confidence=_confidence(draft.get("confidence")),
                risk=str(draft.get("risk") or "medium") or "medium",
                source_coverage_status=_coverage_status(str(draft.get("source_coverage_status") or "")),  # type: ignore[arg-type]
                story_weight=_story_weight(str(draft.get("story_weight") or ""), kind=str(draft.get("kind") or "")),  # type: ignore[arg-type]
                treatment_role_hint=str(draft.get("treatment_role_hint") or ""),
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
        str(item.get("question") or "")
        for item in gaps
        if (
            item.get("status") in {ResearchGapStatus.OPEN.value, ResearchGapStatus.DEFERRED.value}
            and (
                item.get("severity") == ResearchGapSeverity.BLOCKING.value
                or _treatment_relevance(item.get("treatment_relevance")) == ResearchGapTreatmentRelevance.CENTRAL.value
            )
            and str(item.get("question") or "").strip()
        )
    ]
    nonblocking_gap_risks = [
        _risk_from_gap(item)
        for item in gaps
        if (
            item.get("severity") in {ResearchGapSeverity.NON_BLOCKING.value, ResearchGapSeverity.OPTIONAL.value}
            and _treatment_relevance(item.get("treatment_relevance")) != ResearchGapTreatmentRelevance.CENTRAL.value
            and item.get("status") in {ResearchGapStatus.OPEN.value, ResearchGapStatus.DEFERRED.value}
        )
    ]
    structured_risks = [*risks, *nonblocking_gap_risks, *source_quality_risks]
    memo_field_risks = [str(item) for item in fields.get("risk_register") or [] if str(item).strip()]
    if structured_risks:
        memo_field_risks = []

    return ResearchMemo(
        title=title,
        thesis=fields.get("thesis") or f"The episode should investigate {title} through source-grounded mechanisms, stakes, and turns.",
        research_synthesis=(
            fields.get("research_synthesis")
            or fields.get("narrative_spine")
            or "The memo identifies source-grounded findings, context, mechanisms, changes, tensions, drivers, impacts, and unresolved research risks."
        ),
        editorial_atoms=atoms,
        source_coverage=fields.get("source_coverage") or processed_summaries,
        open_questions=_dedupe_normalized(blocking_open_questions),
        risk_register=_dedupe_normalized([*structured_risks, *memo_field_risks]),
    )
