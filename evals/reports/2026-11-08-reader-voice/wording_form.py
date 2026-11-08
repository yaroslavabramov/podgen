"""Diagnostic wording-first form; no production wiring until the controlled readings complete."""

FIELDS = {
    "findings": (
        "First inspect the actual spoken wording, before summarizing or praising the story. "
        "Check grammatical forms and sentence construction, then the combined effect of neighboring phrases. "
        "Report addressed language repairs as well as structural problems. A clear story does not establish "
        "that its wording is ready. Findings may be empty when the words work; do not manufacture a defect "
        "or treat figurative language itself as one."
    ),
    "listener_answer": (
        "After assessing the actual words, reconstruct what the listener can follow and experience. "
        "A useful narrative move can survive a required rewrite of its wording; do not use the move to "
        "certify the wording."
    ),
    "strengths": (
        "What works in these actual words or in the narrative move they serve. Distinguish the two: "
        "a useful image or transition may need to be preserved through a wording repair. "
        "Do not withdraw praise merely because another passage needs correction."
    ),
    "recommendation": (
        "Decision after checking the words and the complete telling. Objective grammatical errors require "
        "correction even when the meaning is clear. Judge cumulative style by its concrete effect, "
        "preserving artistic freedom. Keep is valid when no required repair remains."
    ),
}
FINDING_FIELDS = {
    "observation": (
        "Specific defect in the quoted words or their combined effect on the passage. For grammar, name "
        "the erroneous form or construction. For style, explain what the actual sequence does to the "
        "voice or listening experience, rather than imposing a preference for plainness."
    ),
    "requested_change": (
        "Concrete correction or editorial operation on the addressed words, with the narrative contribution "
        "to preserve. Distinguish a self-contained copyedit from rewriting a passage. A style label alone "
        "is not an actionable repair."
    ),
}
ORDER = [
    "reading_mode",
    "findings",
    "revision_scope",
    "listener_answer",
    "important_limits",
    "research_requests",
    "strengths",
    "duration_note",
    "recommendation",
]


def describe_wording_first(schema):
    props = schema["properties"]
    assert set(props) == set(ORDER)
    for field, description in FIELDS.items():
        props[field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = (
            description
        )
    schema["properties"] = {field: props[field] for field in ORDER}
    schema["required"] = ORDER.copy()
