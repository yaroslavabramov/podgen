"""The narrative reader first reconstructs the listening experience, then commissions the edit."""

ORDER = (
    "reading_mode",
    "listener_answer",
    "revision_scope",
    "findings",
    "strengths",
    "important_limits",
    "research_requests",
    "duration_note",
    "recommendation",
)


def describe_story_edit(schema):
    props = schema["properties"]
    props["listener_answer"]["description"] = (
        "Follow this particular telling from the first thing the listener wants to know to its last "
        "moment. Identify what advances or interrupts the story and how the ending lands."
    )
    props["revision_scope"]["description"] = (
        "Choose the scale needed to deliver that complete story. If changing the order or development "
        "of a connected scene, authorize its inclusive range. A cluster of separate wording edits "
        "cannot authorize that work."
    )
    props["findings"]["description"] = (
        "Commission each change once. Lead with any necessary story edit, giving its intended effect, "
        "the passage to rebuild and what to keep. Follow with independent line repairs. May be empty."
    )
    props["recommendation"]["description"] = (
        "Would you send this telling and its actual words to recording? A required scene edit or a "
        "genuine language error requires revise. Keep a finished episode that works."
    )
    schema["properties"] = {name: props[name] for name in ORDER}
    schema["required"] = list(ORDER)
