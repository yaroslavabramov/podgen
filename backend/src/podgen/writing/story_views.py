"""Lossless semantic views of a saved proposal; v1 is retained for pinned historical runs."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .card_catalog import catalog
from .projection_versions import CURRENT, V1, V2, historical


def selected_labels(plan, packet):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    body = plan["plan"]
    refs = [
        *body["opening"]["material_refs"],
        *(ref for act in body["acts"] for ref in act["material_refs"]),
        *(ref for question in plan["research_requests"] for ref in question["material_refs"]),
    ]
    if set(refs) - set(catalog(packet)):
        raise ContractError("unknown_editorial_support_label")
    return set(refs)


def narrative_decisions(plan, *, projection_version=CURRENT):
    source = plan["plan"]
    if source is None:
        raise ContractError("writing_plan_required")
    if projection_version == V1:
        ending = deepcopy(source["ending"])
        ending.pop("promises_to_close")
        result = dict(ending=ending, duration_strategy=deepcopy(source["duration_strategy"]))
    else:
        result = {"main_question": source["main_question"]} if projection_version not in (V1, V2) else {}
        conclusion = dict(meaning=source["ending"]["answer_to_deliver"])
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
    result["opening"] = {key: deepcopy(source["opening"][key]) for key in ("approach", "question_to_open")}
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
    return result


def story_intent(plan):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    body = plan["plan"]
    return dict(
        answer=body["ending"]["answer_to_deliver"],
        qualifications=deepcopy(body["ending"]["qualifications"]),
        steps=[
            dict(
                question=act["listener_question"],
                change=act["intended_change_in_understanding"],
                transition=deepcopy(act["transitions"]),
            )
            for act in body["acts"]
        ],
    )


def editorial_frame(plan, *, projection_version=CURRENT):
    if plan is None or plan.get("plan") is None:
        raise ContractError("writing_plan_required")
    result = {"main_question": plan["plan"]["main_question"]} if projection_version not in (V1, V2) else {}
    result["listener_promise"] = plan["plan"]["listener_promise"]
    if historical(projection_version):
        result["qualifications"] = deepcopy(plan["plan"]["ending"]["qualifications"])
        result["limitations"] = deepcopy(plan["limitations"])
    result["unresolved_questions"] = [
        dict(question=q["question"], why_needed=q["why_needed"]) for q in plan["research_requests"]
    ]

    return result
