"""Neutral planner field tasks; the system instruction owns the editorial choice of ending."""

FIELDS = {
    "last_moment": (
        "The ending's intended content for the scriptwriter. Leave composition and spoken wording to the writer."
    ),
    "why_end_here": "Why this ending suits the selected story.",
    "promises_to_close": (
        "Questions or promises to resolve during the episode. Their answers need not be repeated in the ending."
    ),
}


PLAN_FIELDS = {
    "working_answer": (
        "The supported answer or meaning established across the whole episode, including "
        "uncertainty that changes it. This is planning meaning, not ready-made spoken lines."
    ),
    "route_summary": (
        "The selected story from its prepared opening through connected developments and the final act. "
        "Select or combine branches that repeat the same contribution; keep event chronology and "
        "the timing of discoveries accurate."
    ),
}


def describe(schema):
    ending = schema["$defs"]["PlanningFinalMoment"]["properties"]
    for name, description in FIELDS.items():
        ending[name]["description"] = description
    proposal = schema["$defs"]["PlanProposalV9"]["properties"]
    for name, description in PLAN_FIELDS.items():
        proposal[name]["description"] = description
