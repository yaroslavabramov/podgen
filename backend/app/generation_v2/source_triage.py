from __future__ import annotations

import re

from app.generation_v2.schemas import QuoteCard, SourceCard, SourceTriageItem, SourceTriageReport


_AUTHORITY_SCORE = {"primary": 3.0, "secondary": 2.0, "tertiary": 0.8, "unknown": 1.0}
_RELIABILITY_SCORE = {"high": 2.0, "medium": 1.0, "unknown": 0.0, "low": -1.0}
_COVERAGE_SCORE = {"ready": 2.0, "partial": 0.5, "failed": -4.0}
_TYPE_SCORE = {"pdf": 0.8, "web": 0.4, "audio": 0.2, "video": 0.2}


def build_source_triage(
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    user_prompt: str,
) -> SourceTriageReport:
    prompt_tokens = _tokens(user_prompt)
    quotes_by_source: dict[str, list[QuoteCard]] = {}
    for quote in quote_cards:
        quotes_by_source.setdefault(quote.source_id, []).append(quote)

    ranked: list[SourceTriageItem] = []
    for card in source_cards:
        score, reasons = _source_score(card, prompt_tokens)
        top_quote_refs = [_quote_ref(quote) for quote in quotes_by_source.get(card.source_id, [])]
        ranked.append(
            SourceTriageItem(
                source_id=card.source_id,
                rank=0,
                score=score,
                authority=card.authority,
                reliability=card.reliability,
                useful_for=card.useful_for,
                top_quote_refs=top_quote_refs,
                relevance_reasons=reasons,
                cautions=card.gaps_or_cautions,
            )
        )

    ranked.sort(key=lambda item: item.score, reverse=True)
    for index, item in enumerate(ranked, start=1):
        item.rank = index

    top_sources_by_function: dict[str, list[str]] = {}
    top_evidence_by_function: dict[str, list[str]] = {}
    for item in ranked:
        for function in item.useful_for or ["background"]:
            top_sources_by_function.setdefault(function, []).append(item.source_id)
            top_evidence_by_function.setdefault(function, []).extend(item.evidence_ids)
    top_sources_by_function = {key: value for key, value in top_sources_by_function.items()}
    top_evidence_by_function = {
        key: _dedupe(value)
        for key, value in top_evidence_by_function.items()
    }

    gaps = _coverage_gaps(top_sources_by_function)
    contradictions = [
        f"{item.source_id}: low reliability source needs framing"
        for item in ranked
        if item.reliability == "low"
    ]
    return SourceTriageReport(
        ranked_sources=ranked,
        top_sources_by_function=top_sources_by_function,
        top_evidence_by_function=top_evidence_by_function,
        gaps=gaps,
        contradictions=contradictions,
    )


def select_sources_for_act(
    *,
    source_triage: SourceTriageReport | None,
    preferred_source_ids: list[str],
    act_text: str,
    minimum: int,
) -> list[str]:
    selected: list[str] = []
    for source_id in preferred_source_ids:
        if source_id and source_id not in selected:
            selected.append(source_id)

    if source_triage is None:
        return selected

    functions = _act_functions(act_text)
    for function in functions:
        for source_id in source_triage.top_sources_by_function.get(function, []):
            if source_id not in selected:
                selected.append(source_id)
            if len(selected) >= minimum:
                break
        if len(selected) >= minimum:
            break

    for item in source_triage.ranked_sources:
        if item.source_id not in selected:
            selected.append(item.source_id)
        if len(selected) >= minimum:
            break
    return selected


def quote_ref(quote: QuoteCard) -> str:
    return _quote_ref(quote)


def _source_score(card: SourceCard, prompt_tokens: set[str]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    score += _AUTHORITY_SCORE.get(card.authority, _AUTHORITY_SCORE["unknown"])
    score += _RELIABILITY_SCORE.get(card.reliability, _RELIABILITY_SCORE["unknown"])
    score += _COVERAGE_SCORE.get(card.coverage_status, 0.0)
    score += _TYPE_SCORE.get(card.source_type, 0.0)
    if card.authority in {"primary", "secondary"}:
        reasons.append(f"{card.authority} source")
    if card.reliability in {"high", "medium"}:
        reasons.append(f"{card.reliability} reliability")
    if card.coverage_status == "ready":
        reasons.append("ready native/provider representation")

    text_tokens = _tokens(" ".join([card.title, card.summary, " ".join(card.useful_for)]))
    overlap = len(prompt_tokens & text_tokens)
    if overlap:
        score += min(3.0, overlap * 0.35)
        reasons.append(f"{overlap} prompt token match(es)")
    if card.useful_for:
        score += min(1.5, len(set(card.useful_for)) * 0.25)
    score -= min(2.0, len(card.gaps_or_cautions) * 0.35)
    score -= min(1.0, card.failed_representation_count * 0.2)
    return round(score, 3), reasons or ["available project source"]


def _coverage_gaps(top_sources_by_function: dict[str, list[str]]) -> list[str]:
    required = ["tension", "mechanism", "impact"]
    return [f"No strong {function} source identified" for function in required if not top_sources_by_function.get(function)]


def _act_functions(text: str) -> list[str]:
    lower = text.lower()
    functions: list[str] = []
    for label, markers in {
        "tension": ("conflict", "tension", "dispute", "stakes", "tradeoff", "trade-off"),
        "mechanism": ("mechanism", "how", "system", "process"),
        "context": ("history", "origin", "before", "timeline", "background"),
        "change": ("turn", "shift", "break", "change", "reform"),
        "impact": ("cost", "impact", "effect", "consequence", "risk", "benefit"),
        "exception": ("exception", "edge", "counterexample"),
        "driver": ("driver", "cause", "pressure", "constraint", "incentive"),
    }.items():
        if any(marker in lower for marker in markers):
            functions.append(label)
    return functions or ["background"]


def _quote_ref(quote: QuoteCard) -> str:
    if quote.evidence_id:
        return quote.evidence_id
    return f"{quote.source_id}:{quote.locator}"


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[^\W_]{3,}", text.lower(), flags=re.UNICODE) if token}


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
