"""Production reader field tasks; the published review shape and order stay unchanged."""

FIELDS = {
    "listener_answer": (
        "Assess the complete listening experience: what draws this audience in, how the story "
        "develops, where it first reaches a meaningful completion, and what the entire later "
        "passage contributes. Include the cumulative effect of the narrator's voice, not only whether "
        "individual sentences are clear or correct."
    ),
    "findings": (
        "Lead with required repairs to the whole telling, recurring stylistic patterns or the "
        "development of a connected passage, then independent line repairs. A repeated device "
        "can weaken the voice even when each occurrence is grammatical. Assess an extended ending "
        "together with its approach and resolution. Anchor the actual problem and commission "
        "each repair once; do not manufacture findings when the text works."
    ),
    "revision_scope": (
        "Choose the scope needed for the required repair, including a recurring pattern or an "
        "ending whose selection or development must change. Authorize the complete connected "
        "range when needed; scattered wording repairs do not authorize rebuilding that passage."
    ),
    "strengths": (
        "Identify contributions worth retaining through revision. An effective image, narrative "
        "move or individual phrase does not establish that the complete voice or ending works; "
        "its contribution may survive in different wording, material or placement."
    ),
    "recommendation": (
        "Would you send this complete telling and its actual words to recording? Require revise "
        "for a demonstrated problem in the story, cumulative voice, ending or grammar, even if "
        "the account is clear and its individual devices are defensible. Keep is valid when no "
        "required repair remains; an equally effective alternative is optional."
    ),
}
FINDING_FIELDS = {
    "observation": (
        "Concrete effect of the anchored words on this audience. For a recurring stylistic "
        "pattern, show how its occurrences work together across the passage or episode. For "
        "an extended ending, identify what the listener gains or loses after the resolution. "
        "A device's name or a preference for plainness does not establish a defect."
    ),
    "requested_change": (
        "Commission the operation that resolves the underlying problem at its needed scale, "
        "with its intended listening effect. For an ending edit, compare the first meaningful "
        "completion with the full later passage, then read the proposed surviving sequence with "
        "its setup and explain why it completes this story. Do not substitute "
        "successive isolated cuts for a needed reconstruction."
    ),
    "preserve": (
        "Supported meaning or useful narrative effect to carry through the repair; may be empty. "
        "Preserve that contribution rather than automatically freezing a praised phrase, "
        "particular object, image or existing last line."
    ),
}


def describe_episode_edit(schema):
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description
