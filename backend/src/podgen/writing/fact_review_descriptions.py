"""Factual comparison semantics for the current review form; no external state."""


def describe_fact_review(schema, *, finding="FactFinding"):
    props = schema["properties"]
    props["listener_answer"]["description"] = (
        "State the material assertions as a listener would hear them. Keep the scope of the spoken wording. An unstated source qualification may affect your assessment."
    )
    props["important_limits"]["description"] = (
        "Private evidence comparison. For consequential numbers, causal links or attributed details in the speech, identify any difference between what was counted, observed or reported and the extent of the spoken claim. A record, sample or witness may cover less than the sentence implies. Findings are for differences that change meaning; these checks are not lines for the narrator."
    )
    props["strengths"]["description"] = "Supported wording that matters to a correction. May be empty."
    f = schema["$defs"][finding]["properties"]
    f["observation"]["description"] = (
        "Exact difference between what the spoken claim implies and what the cited evidence establishes, and why it matters here."
    )
    f["requested_change"]["description"] = (
        "What must become true in the repaired account. Leave the writer free to cut a dispensable claim. When the claim needs to stay, specify the smallest accurate meaning."
    )
    order = [
        "reading_mode",
        "listener_answer",
        "important_limits",
        "findings",
        "research_requests",
        "strengths",
        "duration_note",
        "revision_scope",
        "recommendation",
    ]
    assert set(order) == set(props)
    schema["properties"] = {k: props[k] for k in order}
    schema["required"] = order
