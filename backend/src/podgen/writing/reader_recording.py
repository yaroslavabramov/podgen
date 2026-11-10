"""Recording judgment without a prescribed ending form; listener-experience/9 only."""

FIELDS = {
    "listener_answer": (
        "Assess the complete listening experience: what draws this audience in, how the telling "
        "develops and what the full ending leaves the listener with. Include the cumulative voice "
        "and actual wording. Support a required repair with a concrete defect in this execution, "
        "rather than selecting the earliest possible completion or a preferred ending form."
    ),
    "findings": (
        "Commission changes for demonstrated defects in the actual telling, at the scale needed "
        "to repair them. For a recurring manner or connected passage, commission one coherent repair "
        "before independent line edits. An ordinary summary, authorial attitude, repetition of the main point or "
        "return to earlier events can work, including in the ending. These choices alone do not "
        "require repair. Keep editorial and methodological diagnosis separate from spoken wording. "
        "May be empty."
    ),
    "revision_scope": (
        "Choose the scope needed for the demonstrated repair. Authorize the complete connected "
        "range when its development, selection or ending must change. Removing an unnecessary "
        "branch can resolve a problem without adding an explanation. Use none when no required "
        "repair remains."
    ),
    "strengths": (
        "Identify what works in the actual words and the complete telling. Preserve useful meaning "
        "and listening effects through any required repair; a praised phrase or last line need not "
        "stay fixed. A useful contribution does not establish that every part works."
    ),
    "recommendation": (
        "Would you send this telling and its actual words to recording? Require revise for a "
        "demonstrated defect in execution, including grammar. Keep is valid when no required "
        "repair remains. An equally recordable alternative is optional; a different ending form "
        "or shorter version is not inherently a required improvement."
    ),
}
FINDING_FIELDS = {
    "observation": (
        "Private editorial diagnosis: identify the defect in the anchored words or their combined "
        "effect, and explain its consequence for this audience. For recurring style or an ending, "
        "assess the actual passage in context. For genre commentary, compare the remarks with the "
        "scenes and identify their effect on movement or voice. A device's name, repetition itself or a preferred "
        "form does not establish a defect."
    ),
    "requested_change": (
        "A concrete operation on the addressed telling and its intended listening effect. Read the "
        "surviving sequence with its setup and dependent conclusions. If an unclear branch is "
        "unnecessary because the commissioned question is already understandable without it, "
        "you may request its removal. A scope repair changes the retained account or claim; "
        "it does not automatically require spoken methodology. For a retained explanation with "
        "a missing basis, ask the writer to establish it from the sources or change that account. "
        "Commission the resulting telling rather than a recital of your diagnosis."
    ),
    "preserve": (
        "Supported meaning, essential factual conditions and useful listening effect to retain. "
        "They constrain the repaired account without requiring every boundary or editorial reason "
        "to be spoken. Existing wording or a last line may change while its contribution survives. "
        "May be empty."
    ),
}


def describe_recording(schema):
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description
