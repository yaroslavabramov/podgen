"""Current model-facing plan view; complete historical candidates remain in the journal."""

from copy import deepcopy

from .projection_versions import historical


def candidate_view(candidate, *, projection_version):
    view = deepcopy(candidate)
    if not historical(projection_version):
        view.pop("limitations", None)
        plan = view.get("plan")
        if plan:
            for act in plan["acts"]:
                act.pop("limitations_to_voice", None)
            plan["ending"].pop("qualifications", None)
    return view
