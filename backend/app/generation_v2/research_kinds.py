from __future__ import annotations


ATOM_KIND_DEFINITIONS = {
    "context": "origins, background, setup, chronology",
    "mechanism": "how a process, system, behavior, or phenomenon works",
    "change": "event, decision, crisis, reform, reversal, or shift that changed direction",
    "tension": "conflict, tradeoff, contradiction, disagreement, or competing interpretation",
    "driver": "why something happens or persists: causes, pressures, constraints, incentives, conditions",
    "impact": "effects, consequences, costs, benefits, stakes, downstream risks",
    "exception": "bounded exception to the main pattern",
    "open_question": "material question still unresolved after attempted research",
}

ALLOWED_RESEARCH_ATOM_KINDS = set(ATOM_KIND_DEFINITIONS)
LEGACY_RESEARCH_ATOM_KIND_MAP = {
    "history": "context",
    "turning_point": "change",
    "conflict": "tension",
    "consequence": "impact",
    "human_cost": "impact",
    "institutional_incentive": "driver",
    "unanswered_question": "open_question",
}
ATOM_KIND_GUIDANCE = "; ".join(f"{kind}={meaning}" for kind, meaning in ATOM_KIND_DEFINITIONS.items())
ATOM_KIND_PROMPT_BLOCK = "Atom kinds:\n" + "\n".join(
    f"- {kind} = {meaning}." for kind, meaning in ATOM_KIND_DEFINITIONS.items()
)


def normalize_research_atom_kind(value: str, *, default: str = "open_question") -> str:
    text = str(value or "").strip()
    if text in ALLOWED_RESEARCH_ATOM_KINDS:
        return text
    return LEGACY_RESEARCH_ATOM_KIND_MAP.get(text, default)
