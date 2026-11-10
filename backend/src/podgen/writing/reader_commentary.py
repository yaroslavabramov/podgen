"""Contextual deletion comparisons; listener-experience/11 field tasks."""

FIELDS = {
    "findings": (
        "Commission concrete repairs for demonstrated defects in the actual telling. Test a "
        "candidate explanatory cut in its surrounding speech and identify what understanding "
        "or voice it changes. A joke, aside, reflection, ordinary summary or return can work "
        "without adding information. Repetition alone does not establish a defect. For a "
        "recurring problem, give one coherent repair at the needed scale before independent "
        "line edits. Keep private diagnosis separate from spoken wording. May be empty."
    ),
}

FINDING_FIELDS = {
    "observation": (
        "Private diagnosis of the defect in the anchored words and its effect on this audience. "
        "Assess understanding in listening order. For an explanatory cut, compare the passage "
        "with and without the words, including both joins: what connection, meaning or voice "
        "effect is lost or improved? For grammar, identify the faulty construction. Assess "
        "recurring commentary and the full ending in context; a device's name, repetition "
        "itself or a preferred form does not establish a defect."
    ),
    "requested_change": (
        "A concrete operation and its intended listening effect. For a cut or shortening, name "
        "the exact words and check the surviving sequence with its setup and dependent "
        "conclusions. Preserve a connection the listener needs and useful humor, attitude or "
        "voice. Use the smallest adequate repair; authorize the entire connected change when "
        "needed. An unnecessary unclear branch can be removed. For a retained explanation "
        "with a missing basis, ask the writer to establish it from sources or change that "
        "account. Commission the resulting telling; your diagnosis and source checks do not "
        "automatically become spoken methodology."
    ),
}


def describe_commentary(schema):
    for field, description in FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description


SCENE_FIELDS = {
    "listener_answer": (
        "Assess the complete listening experience, cumulative voice and full ending in actual "
        "words. Before approving a scene with explanatory commentary, establish what listeners "
        "already understand at its last substantive action, result or reply. Compare continuing "
        "through the commentary with the shorter sequence and its next join. Let that comparison "
        "inform readiness and required repairs, including in otherwise successful scenes. "
        "Support a repair with a defect in this execution. An ordinary summary, attitude or "
        "return can complete the telling; the earliest possible stop or a preferred form does "
        "not establish an improvement."
    ),
    "findings": (
        "Check commentary following successful scenes before deciding they are ready. Establish "
        "what the listener has understood by the last substantive action, result or reply, "
        "then compare the longer and shorter sequence in context. When the commentary merely "
        "restates that understanding and weakens pace, voice or the listener's discovery, "
        "commission an exact required cut or shortening even if the scene is understandable "
        "and grammatical. Preserve needed explanation and useful humor, attitude or reflection. "
        "For a recurring problem, use one coherent repair at the needed scale. Private comparisons "
        "are not spoken script. Findings may be empty when no demonstrated defect remains."
    ),
}

SCENE_FINDING_FIELDS = {
    "observation": (
        "Private diagnosis grounded in the words as heard in order. For explanatory commentary, "
        "identify the last substantive words before it, what listeners can already understand "
        "there and what the commentary adds. Compare the actual longer sequence with the "
        "shorter sequence and both joins; explain the resulting gain or loss in understanding, "
        "pace or voice. Praise for the scene's meaning does not settle the quality of its "
        "following explanation. Identify faulty grammar precisely. Repetition or a device's "
        "name alone is not a defect."
    ),
}


def describe_scene_comparison(schema):
    for field, description in SCENE_FIELDS.items():
        schema["properties"][field]["description"] = description
    for field, description in SCENE_FINDING_FIELDS.items():
        schema["$defs"]["EditorialFinding"]["properties"][field]["description"] = description
