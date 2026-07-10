from __future__ import annotations

from app.generation_v2.agents.fallbacks import treatment_act_count_range
from app.generation_v2.pipeline.research_validation import (
    dedupe_strings,
    research_cluster_is_major,
    research_memo_evidence_ids,
    research_source_map_cluster_coverage_issues,
    research_story_coverage,
    validate_research_memo_evidence,
)
from app.generation_v2.schemas import (
    ActDossier,
    EpisodeCritique,
    FactIssue,
    ResearchMemo,
    ShowBibleContext,
    SourceCard,
    Treatment,
)


def merge_source_cards(base_cards: list[SourceCard], override_cards: list[SourceCard]) -> list[SourceCard]:
    overrides_by_source_id = {card.source_id: card for card in override_cards}
    merged: list[SourceCard] = []
    seen: set[str] = set()
    for card in base_cards:
        merged_card = overrides_by_source_id.get(card.source_id, card)
        merged.append(merged_card)
        seen.add(merged_card.source_id)
    for card in override_cards:
        if card.source_id not in seen:
            merged.append(card)
            seen.add(card.source_id)
    return merged


def validate_treatment(
    treatment: Treatment,
    source_cards: list[SourceCard],
    duration_seconds: int,
    *,
    research_memo: ResearchMemo | None = None,
) -> EpisodeCritique:
    source_ids = {card.source_id for card in source_cards if card.coverage_status in {"ready", "partial"}}
    atom_by_ref = (
        {f"A{idx:02d}": atom for idx, atom in enumerate(research_memo.editorial_atoms, start=1)}
        if research_memo is not None
        else {}
    )
    issues: list[str] = []
    min_acts, max_acts = treatment_act_count_range(duration_seconds)
    if not (min_acts <= len(treatment.acts) <= max_acts):
        issues.append(f"Treatment requires {min_acts}-{max_acts} acts for this target duration.")
    for gap in treatment.planning_gaps:
        if str(gap).strip():
            issues.append(f"Treatment planning gap requires research repair: {str(gap).strip()}")
    target_minutes = duration_seconds / 60
    total_minutes = sum(max(0.0, float(act.target_minutes or 0.0)) for act in treatment.acts)
    if treatment.acts and not (target_minutes * 0.85 <= total_minutes <= target_minutes * 1.15):
        issues.append(
            f"Treatment act target_minutes total {total_minutes:.1f} does not match target duration {target_minutes:.1f}."
        )
    for act in treatment.acts:
        if len(set(act.source_focus) & source_ids) < max(1, min(act.source_density_target, 2)):
            issues.append(f"Act {act.act_number} does not meet source density target.")
        if not act.required_atom_refs and duration_seconds >= 2700:
            issues.append(f"Act {act.act_number} lacks required editorial atom anchors.")
        if duration_seconds >= 2700 and len(act.required_evidence_ids) < max(1, min(act.source_density_target, 2)):
            issues.append(f"Act {act.act_number} lacks enough required evidence ids for long-form drafting.")
        if duration_seconds >= 2700 and atom_by_ref:
            selected_atoms = [atom_by_ref[ref] for ref in act.required_atom_refs if ref in atom_by_ref]
            core_atoms = [atom for atom in selected_atoms if atom.story_weight == "core"]
            weak_or_partial = [atom for atom in selected_atoms if atom.source_coverage_status in {"partial", "weak"}]
            caveat_atoms = [atom for atom in selected_atoms if atom.story_weight == "caveat"]
            if not core_atoms:
                issues.append(f"Act {act.act_number} has no core atom anchor.")
            if selected_atoms and selected_atoms[0].story_weight == "caveat":
                issues.append(f"Act {act.act_number} uses a caveat atom as its primary anchor.")
            if caveat_atoms and len(core_atoms) < 1:
                issues.append(f"Act {act.act_number} builds on caveat material without a core atom.")
            if act.target_minutes >= 6 and weak_or_partial and len(core_atoms) < 2:
                issues.append(f"Act {act.act_number} gives major time to partial/weak material without enough core support.")
    return EpisodeCritique(
        passed=not issues,
        summary="Treatment gate passed." if not issues else "Treatment gate failed.",
        weak_sections=issues,
        rewrite_instructions=issues,
    )


def validate_dossier(dossier: ActDossier, source_cards: list[SourceCard]) -> EpisodeCritique:
    known_sources = {card.source_id for card in source_cards if card.coverage_status in {"ready", "partial"}}
    issues = []
    if len(set(dossier.must_use_sources) & known_sources) < dossier.source_density_minimum:
        issues.append("Act dossier does not meet source density minimum.")
    if len(dossier.evidence_ids) < max(1, dossier.source_density_minimum):
        issues.append("Act dossier does not meet evidence density minimum.")
    if not dossier.quote_cards and not dossier.anchor_refs and not dossier.evidence_ids:
        issues.append("Act dossier has no quote cards, anchor refs, or evidence ids.")
    return EpisodeCritique(
        passed=not issues,
        summary="Act dossier gate passed." if not issues else "Act dossier gate failed.",
        weak_sections=issues,
        rewrite_instructions=issues,
    )


def apply_forbidden_phrase_gate(
    critique: EpisodeCritique,
    drafts,
    show_bible: ShowBibleContext,
) -> EpisodeCritique:
    forbidden = [phrase.strip().lower() for phrase in show_bible.forbidden_phrases if isinstance(phrase, str) and phrase.strip()]
    if not forbidden:
        return critique
    text = "\n".join(line.text for draft in drafts for line in draft.lines).lower()
    hits = [phrase for phrase in forbidden if phrase in text]
    if not hits:
        return critique
    return EpisodeCritique(
        passed=False,
        summary=f"{critique.summary} Forbidden phrase hits: {', '.join(hits[:8])}",
        repetition_issues=[*critique.repetition_issues, *[f"Forbidden phrase: {phrase}" for phrase in hits[:8]]],
        pacing_issues=critique.pacing_issues,
        weak_sections=critique.weak_sections,
        rewrite_instructions=[*critique.rewrite_instructions, *[f"Remove forbidden phrase: {phrase}" for phrase in hits[:8]]],
    )


def blocking_fact_issues(issues: list[FactIssue]) -> list[FactIssue]:
    return [
        issue
        for issue in issues
        if issue.severity in {"medium", "high"} or (issue.severity == "low" and issue.blocking)
    ]

__all__ = [
    "dedupe_strings",
    "research_cluster_is_major",
    "research_memo_evidence_ids",
    "research_source_map_cluster_coverage_issues",
    "research_story_coverage",
    "validate_research_memo_evidence",
]
