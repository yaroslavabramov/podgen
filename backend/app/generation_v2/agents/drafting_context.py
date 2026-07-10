from __future__ import annotations

import json

from app.generation_v2.agents.drafting_dossier import (
    build_act_dossier,
)
from app.generation_v2.agents.fallbacks import source_coverage_status as _source_coverage_status
from app.generation_v2.schemas import (
    ActDraft,
    ClaimCitationBundle,
    ContinuityLedger,
    FactIssue,
    FinalTranscript,
    SourceCard,
    Treatment,
)
from app.models.generation_run import GenerationRun


def update_continuity_ledger(ledger: ContinuityLedger, draft: ActDraft) -> ContinuityLedger:
    text = " ".join(line.text for line in draft.lines)
    repeated = list(ledger.repeated_phrases)
    for phrase in (
        "what matters",
        "the real question",
        "at the same time",
        "it turns out",
        "the point is",
        "here's the thing",
        "in other words",
    ):
        if text.lower().count(phrase) > 1 and phrase not in repeated:
            repeated.append(phrase)
    for phrase in draft.repeated_phrase_watchlist:
        if phrase not in repeated:
            repeated.append(phrase)
    claim_candidates = [
        line.text[:240]
        for line in draft.lines
        if any(
            marker in line.text.lower()
            for marker in ("according to", "report", "study", "data", "source", "shows", "found")
        )
    ][:8]
    return ContinuityLedger(
        introduced_entities=[*ledger.introduced_entities, *draft.used_source_ids[:4]][-40:],
        unresolved_promises=[*ledger.unresolved_promises, f"Pay off act {draft.act_number}: {draft.title}"][-20:],
        used_analogies=ledger.used_analogies[-20:],
        repeated_phrases=repeated[-30:],
        citation_sensitive_claims=[*ledger.citation_sensitive_claims, *claim_candidates][-50:],
    )


def final_transcript(
    run: GenerationRun,
    treatment: Treatment,
    drafts: list[ActDraft],
    issues: list[FactIssue],
    bundle: ClaimCitationBundle | None = None,
    source_cards: list[SourceCard] | None = None,
) -> FinalTranscript:
    lines = [line for draft in drafts for line in draft.lines if line.text.strip()]
    claims = bundle.claims if bundle else []
    citation_links = bundle.citation_links if bundle else []
    return FinalTranscript(
        title=treatment.title,
        description=treatment.logline,
        lines=lines,
        quality_metrics={
            "v2_fact_issue_count": len(issues),
            "blocking_fact_issue_count": len([issue for issue in issues if issue.blocking]),
            "remaining_low_fact_issue_count": len([issue for issue in issues if issue.severity == "low"]),
            "supported_claim_count": len([claim for claim in claims if claim.support_level == "supported"]),
            "weak_claim_count": len([claim for claim in claims if claim.support_level == "weak"]),
            "citation_link_count": len(citation_links),
            "source_coverage_status": _source_coverage_status(source_cards or []),
            "v2_dialogue_line_count": len(lines),
            "v2_act_count": len(drafts),
            "verified_fact_pct": 100 if not issues else max(50, 100 - len(issues) * 10),
        },
    )


def json_dumps(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(
            [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value],
            ensure_ascii=False,
        )
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


__all__ = ["build_act_dossier", "final_transcript", "json_dumps", "update_continuity_ledger"]
