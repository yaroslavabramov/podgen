"""Listener-facing schema descriptions, pinned independently of input projections."""

from podgen.domain.errors import ContractError

VERSION = "listener-experience/1"

FIELDS = {
    "listener_answer": (
        "Reconstruct the listening experience from the spoken text: what the opening lets the listener "
        "recognize or become interested in, how interest and understanding develop through the telling, "
        "and what remains at the end. Follow this text's genre; a question-and-answer structure is not required."
    ),
    "important_limits": (
        "Only limits in the supplied text that materially affect this listening experience. "
        "May be empty; do not invent problems or require a list of caveats in the script."
    ),
    "strengths": (
        "Anchored observations about what works for this listener in this genre. "
        "A good passage or complete text may need no change."
    ),
    "findings": (
        "Anchored issues affecting the listener's experience, including impression, selection of material, "
        "spoken language, interest or understanding. May be empty; do not manufacture defects."
    ),
}

FINDING_FIELDS = {
    "observation": (
        "Specific effect of these spoken words on the listener: impression, interest, clarity, "
        "selection, pace or language, as relevant to this text's genre."
    ),
    "requested_change": (
        "Actionable editorial change and its intended effect on this listening experience. "
        "May concern selection, wording, impression or structure as well as understanding; "
        "preserve what works and the text's genre."
    ),
}


def describe_reader(schema, version):
    if version is None:
        return
    if version != VERSION:
        raise ContractError("unknown_reader_review_schema_version")
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description
