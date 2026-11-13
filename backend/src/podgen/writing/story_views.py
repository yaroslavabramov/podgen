"""Lossless semantic views of a saved proposal; v1 is retained for pinned historical runs."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from . import scene_plan_views
from .card_catalog import catalog
from .plan_selection import attach as attach_selection
from .projection_versions import CURRENT, V1, V2, V8, V9, historical, script_handoff
from .scene_plan import is_scene_plan


def selected_labels(plan, packet):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    body = plan["plan"]
    refs = [
        *body["opening"]["material_refs"],
        *(ref for act in body["acts"] for ref in act["material_refs"]),
        *(ref for question in plan["research_requests"] for ref in question["material_refs"]),
    ]
    if packet.contract == "research-editorial-input/1":
        refs.extend(ref for note in plan.get("editorial_notes", []) for ref in note.get("material_refs", []))
    if is_scene_plan(plan):
        refs.extend(body["ending"]["material_refs"])
    if set(refs) - set(catalog(packet)):
        raise ContractError("unknown_editorial_support_label")
    return set(refs)


def narrative_decisions(plan, *, projection_version=CURRENT, plan_selection_contract=None):
    source = plan["plan"]
    if source is None:
        raise ContractError("writing_plan_required")
    if is_scene_plan(plan):
        result = scene_plan_views.story(plan, projection_version)
        attach_selection(result, plan, version=plan_selection_contract, projection=projection_version)
        return result
    if projection_version == V1:
        ending = deepcopy(source["ending"])
        ending.pop("promises_to_close")
        result = dict(ending=ending, duration_strategy=deepcopy(source["duration_strategy"]))
    else:
        result = {"main_question": source["main_question"]} if projection_version not in (V1, V2) else {}
        conclusion = dict(meaning=source["ending"].get("answer_to_deliver", source["working_answer"]))
        if historical(projection_version):
            conclusion["qualifications"] = deepcopy(source["ending"]["qualifications"])
        duration = source["duration_strategy"]
        result.update(
            listener_promise=source["listener_promise"],
            conclusion=conclusion,
            duration_strategy=deepcopy(duration)
            if historical(projection_version)
            else dict(estimated_seconds_range=deepcopy(duration["estimated_seconds_range"])),
        )
    if projection_version == V9:
        result.pop("conclusion", None)
        result["episode_answer"] = source["working_answer"]
        result["ending"] = deepcopy(source["ending"]) if "last_moment" in source["ending"] else None
    elif "last_moment" in source["ending"]:
        raise ContractError("planning_final_moment_requires_v9")
    result["opening"] = {key: deepcopy(source["opening"][key]) for key in ("approach", "question_to_open")}
    if script_handoff(projection_version):
        result["route_summary"] = source["route_summary"]
    act_fields = (
        "title",
        "listener_question",
        "intended_change_in_understanding",
        "prerequisite_concepts",
        "limitations_to_voice",
        "transitions",
        "estimated_seconds_range",
        "priority",
    )
    if not historical(projection_version):
        act_fields = tuple(field for field in act_fields if field != "limitations_to_voice")
    result["acts"] = [{key: deepcopy(act[key]) for key in act_fields} for act in source["acts"]]
    if projection_version in (V8, V9):
        result["authority"] = "revisable_authorial_decisions"
        for act in result["acts"]:
            act["suggested_priority"] = act.pop("priority")
    if projection_version == V1:
        result["host_functions"] = [
            {("speaker" if key == "speaker_id" else key): deepcopy(value) for key, value in host.items()}
            for host in source["host_functions"]
        ]
    else:
        result["speaker_contributions"] = [
            dict(
                speaker=host["speaker_id"],
                contribution=host["contribution"],
                interaction_purpose=host["interaction_guidance"],
                speech_register=host["speech_register"],
            )
            for host in source["host_functions"]
        ]
    if historical(projection_version):
        result["limitations"] = deepcopy(plan["limitations"])
    result["unresolved_questions"] = [
        dict(question=q["question"], why_needed=q["why_needed"]) for q in plan["research_requests"]
    ]
    attach_selection(result, plan, version=plan_selection_contract, projection=projection_version)
    return result


def story_intent(plan):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    body = plan["plan"]
    return dict(
        answer=body["ending"].get("answer_to_deliver", body["working_answer"]),
        qualifications=deepcopy(body["ending"].get("qualifications", [])),
        steps=[
            dict(
                question=act["listener_question"],
                change=act["intended_change_in_understanding"],
                transition=deepcopy(act["transitions"]),
            )
            for act in body["acts"]
        ],
    )


def editorial_frame(plan, *, projection_version=CURRENT, plan_selection_contract=None):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    if is_scene_plan(plan):
        result = scene_plan_views.frame(plan, projection_version)
        attach_selection(result, plan, version=plan_selection_contract, projection=projection_version)
        return result
    result = {"main_question": plan["plan"]["main_question"]} if projection_version not in (V1, V2) else {}
    result["listener_promise"] = plan["plan"]["listener_promise"]
    if script_handoff(projection_version):
        result["route_summary"] = plan["plan"]["route_summary"]
    if projection_version in (V8, V9):
        result["authority"] = "revisable_authorial_decisions"
    if projection_version == V9:
        result["episode_answer"] = plan["plan"]["working_answer"]
        ending = plan["plan"]["ending"]
        result["ending"] = deepcopy(ending) if "last_moment" in ending else None
    if historical(projection_version):
        result["qualifications"] = deepcopy(plan["plan"]["ending"]["qualifications"])
        result["limitations"] = deepcopy(plan["limitations"])
    result["unresolved_questions"] = [
        dict(question=q["question"], why_needed=q["why_needed"]) for q in plan["research_requests"]
    ]

    attach_selection(result, plan, version=plan_selection_contract, projection=projection_version)
    return result
