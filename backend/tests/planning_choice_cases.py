"""Synthetic editorial results emitted only when the actual request offers the new contract."""


def selection():
    return dict(
        first="SYNTHETIC follow one investigation from initial observation to its result.",
        second="SYNTHETIC compare competing explanations through their different predictions.",
        difference="An unfolding investigation versus a comparison organized by predictions.",
        selected="first",
        why_selected="The unfolding investigation makes the supplied decisions intelligible to this audience.",
    )


def answer_requested_selection(output, body):
    if "story_selection" in body["text"]["format"]["schema"].get("properties", {}):
        output.setdefault("story_selection", selection() if output["plan"] else None)
    return output
