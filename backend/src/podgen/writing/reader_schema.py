"""Listener-facing schema descriptions, pinned independently of input projections."""

from podgen.domain.errors import ContractError

V1 = "listener-experience/1"
V2 = "listener-experience/2"
V3 = "listener-experience/3"
V4 = "listener-experience/4"
V5 = "listener-experience/5"
VERSION = "listener-experience/6"

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
    if version not in (V1, V2, V3, V4, V5, VERSION):
        raise ContractError("unknown_reader_review_schema_version")
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description

    if version == V5:
        from .reader_wording import describe_wording_first

        describe_wording_first(schema)
    elif version == VERSION:
        from .reader_story import describe_story_edit

        describe_story_edit(schema)


def feedback_model(version):
    from .roles import EditorialFeedback

    if version in (None, V1):
        return EditorialFeedback
    if version == V2:
        from .reader_feedback import ReaderFeedback

        return ReaderFeedback
    if version == V3:
        from .revision.scopes import ReaderAssessment

        return ReaderAssessment
    if version in (V4, V5, VERSION):
        from .revision.scopes import ReaderAssessmentV4

        return ReaderAssessmentV4
    raise ContractError("unknown_reader_review_schema_version")


def snapshot_model(value):
    """A supplied snapshot selects its published shape; run output uses the manifest pin."""
    if "revision_scope" not in value:
        return feedback_model(V1)
    scope = value["revision_scope"]
    if isinstance(scope, dict) and "dependent_passages" in scope:
        return feedback_model(V3 if scope["scale"] == "sequence" and "sequence_ranges" not in scope else V4)
    return feedback_model(V2)
