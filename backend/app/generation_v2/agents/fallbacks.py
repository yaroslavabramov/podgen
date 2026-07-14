from __future__ import annotations

from app.config import settings
from app.generation_v2.agents.citation_fallbacks import fallback_claims, fallback_support_judgments
from app.generation_v2.agents.fallback_research import fallback_research_memo, source_coverage_status
from app.generation_v2.runtime import V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import (
    ActDossier,
    ActDraft,
    DialogueLine,
    EpisodeCritique,
    FactIssue,
    ResearchMemo,
    Treatment,
    TreatmentAct,
)
from app.models.generation_run import GenerationRun


def allow_runtime_fallback(runtime: V2Runtime, phase: str, agent_name: str, exc: V2RuntimeUnavailable) -> None:
    if not settings.generation_v2_allow_runtime_fallback:
        raise exc
    runtime.mark_fallback_used(phase, agent_name)


def fallback_treatment(run: GenerationRun, memo: ResearchMemo, act_count: int) -> Treatment:
    duration_minutes = int((run.config or {}).get("duration_target", 1800)) / 60
    per_act = max(3.0, duration_minutes / max(1, act_count))
    acts = []
    for idx in range(1, act_count + 1):
        atom = memo.editorial_atoms[(idx - 1) % len(memo.editorial_atoms)]
        acts.append(
            TreatmentAct(
                act_number=idx,
                title=atom.title,
                story_role=atom.kind.replace("_", " "),
                opening_question=f"What does this reveal about {memo.title}?",
                main_turn=atom.explanation,
                ending_turn=f"The next layer changes how we understand {memo.title}.",
                source_focus=atom.source_ids,
                target_minutes=per_act,
                required_atom_refs=[f"A{((idx - 1) % len(memo.editorial_atoms)) + 1:02d}"],
                required_quote_refs=list(dict.fromkeys(ref for ref in atom.quote_refs if ref)),
                required_evidence_ids=atom.evidence_ids,
                source_density_target=max(1, min(3, len(atom.source_ids) or 1)),
            )
        )
    return Treatment(
        title=memo.title,
        logline=memo.thesis,
        host_dynamic="Host A drives the narrative; Host B challenges assumptions and asks mechanism questions.",
        acts=acts,
        recurring_questions=["What is the mechanism?", "Who pays the price?", "What changes after the turn?"],
        style_constraints=["Avoid generic podcast filler.", "Keep claims source-grounded."],
    )


def fallback_act_draft(run: GenerationRun, act: TreatmentAct, dossier: ActDossier) -> ActDraft:
    line_count = max(8, min(36, int(act.target_minutes * 2)))
    lines: list[DialogueLine] = []
    source_hint = ", ".join(dossier.must_use_sources[:2]) or "the available source set"
    for idx in range(line_count):
        speaker = "A" if idx % 2 == 0 else "B"
        if idx == 0:
            text = f"Let's open this act with the question: {act.opening_question}"
        elif idx == line_count - 1:
            text = act.ending_turn
        elif speaker == "A":
            text = f"The useful source trail here points to {source_hint}, and the key turn is this: {act.main_turn[:220]}"
        else:
            quote = dossier.quote_cards[idx % len(dossier.quote_cards)].quote if dossier.quote_cards else act.main_turn
            text = f"So the important detail is not just the fact itself, but what it explains: {quote[:240]}"
        lines.append(DialogueLine(speaker=speaker, text=text))
    return ActDraft(
        act_number=act.act_number,
        title=act.title,
        lines=lines,
        used_source_ids=dossier.must_use_sources,
        repeated_phrase_watchlist=[],
    )


def fallback_rewrite_act_draft(draft: ActDraft, critique: EpisodeCritique, fact_issues: list[FactIssue]) -> ActDraft:
    repair_notes = [issue.repair for issue in fact_issues if issue.act_number == draft.act_number] if fact_issues else []
    if not repair_notes and not critique.rewrite_instructions:
        return draft
    revised_lines = list(draft.lines)
    if repair_notes:
        revised_lines.append(
            DialogueLine(
                speaker="B",
                text="Where the source trail is not strong enough, we should treat this as an open question rather than a settled claim.",
            )
        )
    return ActDraft(
        act_number=draft.act_number,
        title=draft.title,
        lines=revised_lines,
        used_source_ids=draft.used_source_ids,
        repeated_phrase_watchlist=draft.repeated_phrase_watchlist[-20:],
    )


def treatment_act_count_range(duration_seconds: int) -> tuple[int, int]:
    minutes = duration_seconds / 60
    if minutes <= 20:
        return (2, 3)
    if minutes <= 30:
        return (3, 4)
    if minutes <= 45:
        return (4, 6)
    if minutes < 60:
        return (5, 7)
    if minutes < 75:
        return (6, 8)
    return (7, 9)


def fallback_act_count(act_count_range: tuple[int, int]) -> int:
    return max(act_count_range[0], round((act_count_range[0] + act_count_range[1]) / 2))

__all__ = [
    "fallback_claims",
    "fallback_support_judgments",
    "fallback_research_memo",
    "source_coverage_status",
]
