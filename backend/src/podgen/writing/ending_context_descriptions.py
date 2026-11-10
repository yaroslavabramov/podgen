"""Contextual ending decisions for story-resolution/4; no new output fields or required technique."""

WHY = (
    "What this audience already understands after the final act, and why the selected ending's "
    "content completes the telling at that point."
)
REVIEW_FIELDS = {
    "listener_answer": (
        "Follow the selected story as a newcomer: its opening premises, developments, first "
        "disclosures and chronology of actions and knowledge. Compare the proposed ending with "
        "what the preceding material, including the final act, already makes clear. Assess the "
        "effect of this particular closing content in this story; naming its technique does not "
        "establish that effect."
    ),
    "findings": (
        "Addressed defects in the selected story, novice prerequisites, opening promise, "
        "chronological implications, overlapping branches or ending. For an ending repair, "
        "identify the concrete defect in its content or relationship to the preceding passage. "
        "A summary, reflection or return can work; new information or a particular emotion is "
        "not required. Empty is valid when the plan works."
    ),
}
REQUESTED_CHANGE = (
    "Actionable repair of the underlying editorial problem; distinguish a proposed remedy from "
    "a new client requirement. An ending problem may call for a coherent replacement of the "
    "ending or its transition. Specify the intended content and useful effect to retain, "
    "addressing dependent passages together when needed; leave composition and phrasing to the writer."
)


def describe(schema, stage):
    if stage in ("plan", "plan-revise"):
        schema["$defs"]["PlanningFinalMoment"]["properties"]["why_end_here"]["description"] = WHY
    elif stage.startswith("plan-review"):
        for field, description in REVIEW_FIELDS.items():
            schema["properties"][field]["description"] = description
        schema["$defs"]["PlanReviewFinding"]["properties"]["requested_change"]["description"] = (
            REQUESTED_CHANGE
        )
