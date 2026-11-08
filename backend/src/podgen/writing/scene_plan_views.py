"""Explicit writing views of a scene-development plan; historical plans have their own renderer."""

from copy import deepcopy

from podgen.domain.errors import ContractError

from .projection_versions import V9


def check_projection(projection):
    if projection != V9:
        raise ContractError("scene_plan_requires_v9")


def frame(output, projection):
    check_projection(projection)
    plan = output["plan"]
    return dict(
        main_question=plan["main_question"],
        listener_promise=plan["listener_promise"],
        episode_answer=plan["working_answer"],
        authority="revisable_authorial_decisions",
        unresolved_questions=[
            dict(question=q["question"], why_needed=q["why_needed"]) for q in output["research_requests"]
        ],
    )


def scenes(plan):
    return dict(
        opening=dict(development=plan["opening"]["development"]),
        acts=[
            dict(
                title=act["title"],
                development=act["development"],
                estimated_seconds_range=deepcopy(act["estimated_seconds_range"]),
                suggested_priority=act["priority"],
            )
            for act in plan["acts"]
        ],
        ending=dict(development=plan["ending"]["development"]),
    )


def story(output, projection):
    plan = output["plan"]
    return dict(
        **frame(output, projection),
        **scenes(plan),
        duration_strategy=dict(
            estimated_seconds_range=deepcopy(plan["duration_strategy"]["estimated_seconds_range"])
        ),
        speaker_contributions=[
            dict(
                speaker=host["speaker_id"],
                contribution=host["contribution"],
                interaction_purpose=host["interaction_guidance"],
                speech_register=host["speech_register"],
            )
            for host in plan["host_functions"]
        ],
    )
