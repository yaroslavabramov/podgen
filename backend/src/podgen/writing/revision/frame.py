"""Versioned author views of the plan and current composition; archived state stays complete."""

from copy import deepcopy

from .. import author_plan
from ..story_views import editorial_frame

FIELDS = ("authority", "main_question", "listener_promise", "route_summary", "unresolved_questions")


def frame_view(
    plan,
    composition,
    *,
    projection_version,
    plan_selection_contract,
    author_plan_handoff,
    prompt_cache_layout,
    scoped,
    compact,
    mode,
):
    starting = (
        (
            author_plan.story(plan)
            if author_plan_handoff
            else editorial_frame(
                plan, projection_version=projection_version, plan_selection_contract=plan_selection_contract
            )
        )
        if plan
        else None
    )
    if starting is not None and scoped:
        starting["authority"] = "revisable_authorial_decisions"
    result = dict(editorial_frame=starting) if starting is not None else {}
    if compact:
        if starting is None and composition is None:
            return result
        frame = {key: deepcopy(starting[key]) for key in FIELDS if starting is not None and key in starting}
        if starting is None:
            frame["authority"] = "revisable_authorial_decisions"
        if composition is not None:
            frame.update(
                {
                    key: deepcopy(composition[key])
                    for key in FIELDS
                    if key != "authority" and key in composition
                }
            )
        result["editorial_frame"] = frame
        if mode == "structural" and starting is not None:
            result["accepted_plan"] = starting
        return result
    # The exact earlier layout and field order are part of historical request reproduction.
    if starting is not None and prompt_cache_layout is not None:
        result["accepted_plan"] = deepcopy(starting)
        if composition is None:
            result["editorial_frame"] = {key: deepcopy(starting[key]) for key in FIELDS if key in starting}
    if composition is not None:
        if author_plan_handoff and plan and prompt_cache_layout is None:
            result["accepted_plan"] = result["editorial_frame"]
        questions = deepcopy(result.get("editorial_frame", {}).get("unresolved_questions", []))
        result["editorial_frame"] = dict(
            authority="revisable_authorial_decisions",
            unresolved_questions=questions,
            **{key: deepcopy(value) for key, value in composition.items() if key != "unresolved_questions"},
        )
        if "unresolved_questions" in composition:
            result["editorial_frame"]["unresolved_questions"] = deepcopy(composition["unresolved_questions"])
    return result
