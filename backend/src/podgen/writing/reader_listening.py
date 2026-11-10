"""Listening order and proportionate commissions; addenda to listener-experience/9."""

FIELDS = {
    "listener_answer": (
        " Track what this listener can understand as the speech unfolds. Check whether the "
        "connection needed to follow a reference or inference is available at that point, "
        "without silently supplying later knowledge. A deliberate question or delayed reveal "
        "can leave its answer open while making clear what the listener is following."
    ),
    "revision_scope": (
        " Choose the smallest coherent operation that resolves the demonstrated defect: words, "
        "a self-contained sentence or a connected passage as needed. For an ending rewrite, "
        "authorize its entire needed development through the intended stopping point and any "
        "dependent setup. A self-contained wording correction within an ending can remain local."
    ),
}

FINDING_FIELDS = {
    "observation": (
        " Assess clarity at the point these words are heard, using the preceding speech rather "
        "than silently supplying later knowledge. Identify what the listener can already follow "
        "and the specific missing connection that causes the defect, if there is one."
    ),
    "requested_change": (
        " Match the operation to the defect. A word or sentence may repair a reference or "
        "transition; request connected rewriting when the development or dependencies need "
        "to change together. Check the resulting speech in listening order and ensure your "
        "revision_scope actually permits the whole requested change."
    ),
}


def describe_listening(schema):
    for field, addition in FIELDS.items():
        schema["properties"][field]["description"] += addition
    for field, addition in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] += addition
