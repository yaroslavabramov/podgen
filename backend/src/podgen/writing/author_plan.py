"""Author-facing decisions from the approved plan, pinned for both writing stages."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .behavior import PRODUCTION
from .projection_versions import V9, manifest_version
from .story_views import narrative_decisions

KEY = "author_plan_handoff"
VERSION = "approved-story/1"


def from_manifest(manifest):
    value = manifest.get(KEY)
    if value is None and manifest.get("behavior_profile") == PRODUCTION:
        raise ContractError("missing_author_plan_handoff")
    if value is not None and (value != VERSION or manifest_version(manifest) != V9):
        raise ContractError("unsupported_author_plan_handoff")
    return value


def story(proposal):
    """Keep approved choices and their evidence links. Rejected pitches and review notes stay private."""
    if not proposal or not proposal.get("plan"):
        raise ContractError("writing_plan_required")
    plan = proposal["plan"]
    view = narrative_decisions(proposal, projection_version=V9)
    view["duration_strategy"] = deepcopy(plan["duration_strategy"])
    view["ending"] = deepcopy(plan["ending"])
    view["opening"]["material_refs"] = list(plan["opening"]["material_refs"])
    for act, source in zip(view["acts"], plan["acts"], strict=True):
        for field in (
            "act_id",
            "material_refs",
            "promises_opened",
            "promises_addressed",
            "limitations_to_voice",
        ):
            if field in source:
                act[field] = deepcopy(source[field])
    if selection := proposal.get("story_selection"):
        view["selected_story"] = dict(
            concept=selection[selection["selected"]], why_selected=selection["why_selected"]
        )
    if proposal.get("limitations"):
        view["limitations"] = deepcopy(proposal["limitations"])
    view["unresolved_questions"] = deepcopy(proposal["research_requests"])
    return view
