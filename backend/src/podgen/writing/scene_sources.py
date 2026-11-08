"""Join accepted editorial choices to the existing author evidence, without inferring script addresses."""

from .card_catalog import catalog
from .model_references import material_aliases
from .scene_plan import is_scene_plan
from .scene_plan_views import scenes


def link_scene_sources(frame, plan, packet, cards, *, draft):
    aliases = material_aliases(catalog(packet))
    identity = "card_id" if packet.contract == "editorial-input/2" else "label"
    for card in cards:
        card["ref"] = aliases[card.pop(identity)]

    body = plan["plan"]

    def refs(scene):
        return [aliases[key] for key in scene["material_refs"]]

    if is_scene_plan(plan):
        view = frame if draft else scenes(body)
        for key in ("opening", "ending"):
            view[key]["support_refs"] = refs(body[key])
        for scene, act in zip(view["acts"], body["acts"], strict=True):
            scene["support_refs"] = refs(act)
        if not draft:
            frame["accepted_plan_scenes"] = view
        return

    if draft:
        frame["opening"]["support_refs"] = refs(body["opening"])
        for scene, act in zip(frame["acts"], body["acts"], strict=True):
            scene["support_refs"] = refs(act)
        return

    frame["accepted_plan_scenes"] = dict(
        guidance=(
            "The accepted plan's emphasis and supporting material. Follow subsequent composition "
            "decisions and the current edit where they change this outline."
        ),
        opening=dict(approach=body["opening"]["approach"], support_refs=refs(body["opening"])),
        acts=[
            dict(
                title=act["title"],
                intended_change_in_understanding=act["intended_change_in_understanding"],
                support_refs=refs(act),
            )
            for act in body["acts"]
        ],
    )
