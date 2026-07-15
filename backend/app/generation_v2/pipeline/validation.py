from __future__ import annotations

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
    valid_evidence_ids: set[str] | None = None,
    valid_quote_refs: set[str] | None = None,
) -> EpisodeCritique:
    source_ids = {card.source_id for card in source_cards if card.coverage_status in {"ready", "partial"}}
    atom_by_ref = (
        {f"A{idx:02d}": atom for idx, atom in enumerate(research_memo.editorial_atoms, start=1)}
        if research_memo is not None
        else {}
    )
    issues: list[str] = []
    for field_name, value in (
        ("title", treatment.title),
        ("logline", treatment.logline),
        ("host_dynamic", treatment.host_dynamic),
    ):
        if not str(value or "").strip():
            issues.append(f"Treatment {field_name} must not be empty.")
    if not treatment.acts:
        issues.append("Treatment must contain at least one act.")
    if research_memo is not None:
        if treatment.supported_answer != research_memo.thesis:
            issues.append("Treatment must preserve the research memo supported answer exactly.")
    actual_act_numbers = [act.act_number for act in treatment.acts]
    expected_act_numbers = list(range(1, len(treatment.acts) + 1))
    if actual_act_numbers != expected_act_numbers:
        issues.append("Treatment act numbers must be unique and sequential starting at 1.")
    act_by_number = {act.act_number: act for act in treatment.acts}
    for gap in treatment.planning_gaps:
        if str(gap).strip():
            issues.append(f"Treatment planning gap requires research repair: {str(gap).strip()}")
    for act in treatment.acts:
        for field_name, value in (
            ("title", act.title),
            ("story_role", act.story_role),
            ("opening_question", act.opening_question),
            ("main_turn", act.main_turn),
            ("ending_turn", act.ending_turn),
        ):
            if not str(value or "").strip():
                issues.append(f"Act {act.act_number} {field_name} must not be empty.")
        if float(act.target_minutes or 0.0) <= 0:
            issues.append(f"Act {act.act_number} target_minutes must be positive.")
        if research_memo is not None and act.episode_supported_answer != research_memo.thesis:
            issues.append(f"Act {act.act_number} lost the episode supported answer.")
        unknown_source_ids = sorted({source_id for source_id in act.source_focus if source_id not in source_ids})
        if unknown_source_ids:
            issues.append(f"Act {act.act_number} references unknown or unavailable sources: {unknown_source_ids}.")
        if not act.required_atom_refs:
            issues.append(f"Act {act.act_number} lacks required editorial atom anchors.")
        duplicate_atom_refs = sorted(
            {ref for ref in act.required_atom_refs if act.required_atom_refs.count(ref) > 1}
        )
        if duplicate_atom_refs:
            issues.append(f"Act {act.act_number} repeats editorial atom refs: {duplicate_atom_refs}.")
        unknown_atom_refs = sorted({ref for ref in act.required_atom_refs if ref not in atom_by_ref})
        if unknown_atom_refs:
            issues.append(f"Act {act.act_number} references unknown editorial atoms: {unknown_atom_refs}.")
        anchor_refs = [anchor.atom_ref for anchor in act.atom_anchors]
        if anchor_refs != [ref for ref in act.required_atom_refs if ref in atom_by_ref]:
            issues.append(f"Act {act.act_number} atom anchors do not match required_atom_refs.")
        for anchor in act.atom_anchors:
            atom = atom_by_ref.get(anchor.atom_ref)
            if atom is None:
                continue
            expected_anchor = {
                "atom_ref": anchor.atom_ref,
                "kind": str(atom.kind),
                "title": atom.title,
                "claim": atom.claim or atom.explanation,
                "story_value": atom.story_value,
                "story_weight": str(atom.story_weight),
            }
            if anchor.model_dump(mode="json") != expected_anchor:
                issues.append(
                    f"Act {act.act_number} atom anchor {anchor.atom_ref} does not preserve the research atom."
                )
        selected_atoms = [atom_by_ref[ref] for ref in act.required_atom_refs if ref in atom_by_ref]
        selected_evidence_ids = {
            evidence_id for atom in selected_atoms for evidence_id in atom.evidence_ids if evidence_id
        }
        selected_quote_refs = {quote_ref for atom in selected_atoms for quote_ref in atom.quote_refs if quote_ref}
        unrelated_evidence_ids = sorted(
            {evidence_id for evidence_id in act.required_evidence_ids if evidence_id not in selected_evidence_ids}
        )
        unrelated_quote_refs = sorted(
            {quote_ref for quote_ref in act.required_quote_refs if quote_ref not in selected_quote_refs}
        )
        stale_evidence_ids = sorted(
            {
                evidence_id
                for evidence_id in act.required_evidence_ids
                if valid_evidence_ids is not None and evidence_id not in valid_evidence_ids
            }
        )
        stale_quote_refs = sorted(
            {
                quote_ref
                for quote_ref in act.required_quote_refs
                if valid_quote_refs is not None and quote_ref not in valid_quote_refs
            }
        )
        if unrelated_evidence_ids:
            issues.append(
                f"Act {act.act_number} references evidence not attached to its editorial atoms: "
                f"{unrelated_evidence_ids}."
            )
        if unrelated_quote_refs:
            issues.append(
                f"Act {act.act_number} references quotes not attached to its editorial atoms: {unrelated_quote_refs}."
            )
        if stale_evidence_ids:
            issues.append(
                f"Act {act.act_number} references evidence outside the current run corpus view: {stale_evidence_ids}."
            )
        if stale_quote_refs:
            issues.append(
                f"Act {act.act_number} references quotes that are not exact evidence in the current run: "
                f"{stale_quote_refs}."
            )
    if research_memo is not None:
        core_atom_refs = {
            ref for ref, atom in atom_by_ref.items() if str(atom.story_weight) == "core"
        }
        disposition_refs = [item.atom_ref for item in treatment.core_atom_dispositions]
        duplicate_dispositions = sorted(
            {ref for ref in disposition_refs if disposition_refs.count(ref) > 1}
        )
        missing_dispositions = sorted(core_atom_refs - set(disposition_refs))
        unexpected_dispositions = sorted(set(disposition_refs) - core_atom_refs)
        if duplicate_dispositions:
            issues.append(f"Core editorial atoms have duplicate dispositions: {duplicate_dispositions}.")
        if missing_dispositions:
            issues.append(f"Core editorial atoms lack an explicit disposition: {missing_dispositions}.")
        if unexpected_dispositions:
            issues.append(f"Non-core or unknown atoms have core dispositions: {unexpected_dispositions}.")
        used_in_acts = {
            ref: {act.act_number for act in treatment.acts if ref in act.required_atom_refs}
            for ref in core_atom_refs
        }
        for item in treatment.core_atom_dispositions:
            if item.atom_ref not in core_atom_refs:
                continue
            if item.disposition == "used":
                if item.act_number not in used_in_acts[item.atom_ref]:
                    issues.append(
                        f"Core atom {item.atom_ref} is marked used in an act that does not reference it."
                    )
                if item.act_number not in act_by_number:
                    issues.append(f"Core atom {item.atom_ref} references an unknown act_number.")
            else:
                if used_in_acts[item.atom_ref]:
                    issues.append(f"Core atom {item.atom_ref} is marked omitted but is used by an act.")
                if item.act_number is not None:
                    issues.append(f"Omitted core atom {item.atom_ref} must not carry an act_number.")
                if not str(item.reason or "").strip():
                    issues.append(f"Omitted core atom {item.atom_ref} requires an editorial reason.")
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
    forbidden = [
        phrase.strip().lower() for phrase in show_bible.forbidden_phrases if isinstance(phrase, str) and phrase.strip()
    ]
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
        rewrite_instructions=[
            *critique.rewrite_instructions,
            *[f"Remove forbidden phrase: {phrase}" for phrase in hits[:8]],
        ],
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
