"""Versioned editorial commissions distinguish the spoken repair from its private diagnosis."""

FIELDS = {
    "findings": (
        "Commission concrete changes to the telling at the required scale. Explain the defect in "
        "observation, give the narrative operation and intended result in requested_change, and state "
        "the supported meaning or useful effect to retain in preserve. The editor's evidential or "
        "methodological analysis is not automatically wording for the speakers. May be empty."
    ),
}

FINDING_FIELDS = {
    "observation": (
        "Private editorial diagnosis: the quoted words, their effect on this listener and the basis "
        "for requiring a change. Distinguish this analysis from what the revised episode should say."
    ),
    "requested_change": (
        "A concrete operation on the addressed passage and the intended spoken development: what "
        "to select, connect, reveal, move or rewrite. For a missing basis, ask the writer to establish "
        "it from the sources or change the unsupported comparison or explanation. Commission the "
        "resulting telling rather than a recital of your diagnosis or a generic qualification. "
        "For an ending, specify the surviving development and needed connected range."
    ),
    "preserve": (
        "Supported meaning, essential factual conditions and useful listening effect to retain. "
        "These constrain the repaired account without requiring a spoken explanation of every "
        "boundary. A current phrase, object or last line may change while its contribution survives. "
        "May be empty."
    ),
}


def describe_commission(schema):
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description
