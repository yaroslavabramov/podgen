"""Field contents only; the system instructions own editorial principles and ending choices."""

PLANNER_ENDING = {
    "last_moment": "Selected ending content for the writer; composition and spoken wording remain the writer's task.",
    "why_end_here": "Why this content fits the whole story and what listeners already understand from the preceding passage.",
    "promises_to_close": "Questions answered during the episode; their answers need not be repeated in the ending.",
}
PLAN_REVIEW_FIELDS = {
    "listener_answer": "The listening experience enabled by the selected plan: opening premises, developments, disclosures, chronology and ending.",
    "findings": "Addressed defects and optional improvements in the proposed telling, with the changes they need. May be empty.",
}
PLAN_REQUESTED_CHANGE = "Concrete change to the addressed plan passage, its intended listening effect and any dependent passages to change together."
READER_FIELDS = {
    "listener_answer": "The complete listening experience in speech order: understanding, interest, cumulative voice and ending.",
    "findings": "Anchored defects and optional improvements in the actual spoken text, at the required repair scale. May be empty.",
    "revision_scope": "Editable scale and ranges needed for the required changes, including dependent passages. Use none when no required repair remains.",
    "strengths": "Anchored wording or narrative contributions and useful listening effects to retain.",
    "recommendation": "Recording readiness based on required repairs; keep when none remain.",
}
READER_FINDING_FIELDS = {
    "observation": "Specific defect in the anchored words and its effect in listening order. For grammar, identify the faulty form or construction.",
    "requested_change": "Concrete operation on the addressed words or passage, its intended listening effect and any dependent joins. Editorial diagnosis is not proposed spoken wording.",
    "preserve": "Supported meaning, necessary factual conditions and useful listening effects to retain. May be empty.",
}


def describe_planning(schema, stage):
    if stage in ("plan", "plan-revise"):
        ending = schema["$defs"]["PlanningFinalMoment"]["properties"]
        for field, description in PLANNER_ENDING.items():
            ending[field]["description"] = description
    elif stage.startswith("plan-review"):
        for field, description in PLAN_REVIEW_FIELDS.items():
            schema["properties"][field]["description"] = description
        schema["$defs"]["PlanReviewFinding"]["properties"]["requested_change"]["description"] = (
            PLAN_REQUESTED_CHANGE
        )


def describe_reader(schema):
    for field, description in READER_FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in READER_FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description
