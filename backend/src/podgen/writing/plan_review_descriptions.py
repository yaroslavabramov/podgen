"""V8 reviews the story a selected plan permits, not only its factual explanation."""

FIELDS = {
    "listener_answer": (
        "Reconstruct the story this exact sequence would let the audience follow: what listeners know "
        "before each discovery, how interest and understanding develop, what the ending leaves them with, "
        "and what is explained again after it is already clear. Compare the planner's added editorial "
        "question and emphasis with the client's actual request; the chosen plan is revisable."
    ),
    "strengths": (
        "Addressed properties that help this particular story and genre work. Explain their contribution; "
        "praise does not freeze the current selection or sequence."
    ),
    "findings": (
        "Addressed editorial problems in the proposed telling, including selection, order of disclosure, "
        "repetition and an added task that displaces the client request. A factually correct act can still "
        "need repair. May be empty when the plan works; do not manufacture a problem or caveat."
    ),
    "decision_reason": (
        "Concrete reason for the decision following from the preceding story assessment and addressed "
        "findings, including their consequence for this audience and request."
    ),
    "decision": (
        "Decision reached after assessing the story enabled by this plan. Factual accuracy alone does "
        "not establish that its chosen telling works; a good plan need not be changed."
    ),
}


def describe_plan_review(schema):
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    definitions = schema["$defs"]
    definitions["PlanReviewStrength"]["properties"]["reason"]["description"] = (
        "What this addressed choice contributes to the story and listening experience; wording and position remain revisable."
    )
    for field, description in {
        "observation": "Specific effect of this choice on the story the listener would receive, whether or not its facts are correct.",
        "requested_change": "Actionable repair of the underlying editorial problem; distinguish a proposed remedy from a new client requirement.",
        "preserve": "Useful story properties, supported meaning or necessary factual conditions to retain through the repair; may be empty.",
    }.items():
        definitions["PlanReviewFinding"]["properties"][field]["description"] = description
    properties = schema["properties"]
    schema["properties"] = {
        **{key: value for key, value in properties.items() if key not in ("decision_reason", "decision")},
        "decision_reason": properties["decision_reason"],
        "decision": properties["decision"],
    }
