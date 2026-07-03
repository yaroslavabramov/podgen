from __future__ import annotations


ATOM_KIND_DEFINITIONS = {
    "history": "origins, background, chronology",
    "mechanism": "how a system/process/arrangement works: actors, rules, dependencies, constraints",
    "turning_point": "event, decision, crisis, reform, or reversal that changed direction",
    "conflict": "tension, disagreement, competing interpretation, or contested claim",
    "institutional_incentive": "why actors/institutions preferred, accepted, resisted, or preserved an arrangement",
    "consequence": "effects, tradeoffs, costs, benefits, downstream risks",
    "exception": "bounded exception to the main pattern",
    "human_cost": "concrete social, market, political, or personal harm/pressure",
    "unanswered_question": "material question still unresolved after attempted research",
}

ALLOWED_RESEARCH_ATOM_KINDS = set(ATOM_KIND_DEFINITIONS)
ATOM_KIND_GUIDANCE = "; ".join(f"{kind}={meaning}" for kind, meaning in ATOM_KIND_DEFINITIONS.items())
ATOM_KIND_PROMPT_BLOCK = "Atom kinds:\n" + "\n".join(
    f"- {kind} = {meaning}." for kind, meaning in ATOM_KIND_DEFINITIONS.items()
)
