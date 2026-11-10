"""Production ending tasks, independently pinned from the unchanged planning shape."""

from podgen.domain.errors import ContractError

from .projection_versions import V9, manifest_version

KEY = "planning_ending_guidance"
LEGACY_VERSION = "story-resolution/1"
SPEECH_VERSION = "story-resolution/2"
NEUTRAL_VERSION = "story-resolution/3"
CONTEXT_VERSION = "story-resolution/4"
VERSION = "story-resolution/5"


def validate(version, projection):
    if version is not None and (
        version not in (LEGACY_VERSION, SPEECH_VERSION, NEUTRAL_VERSION, CONTEXT_VERSION, VERSION)
        or projection != V9
    ):
        raise ContractError("unsupported_planning_ending_guidance")
    return version


def from_manifest(manifest):
    version = validate(manifest.get(KEY), manifest_version(manifest))
    if version is not None and manifest.get("behavior_profile") != "production/1":
        raise ContractError("unsupported_planning_ending_guidance")
    return version


def describe(schema, stage, version):
    if version is None:
        return
    if version in (SPEECH_VERSION, NEUTRAL_VERSION, CONTEXT_VERSION, VERSION):
        from .closing_speech import describe as describe_closing_speech

        describe_closing_speech(schema, stage)
        if version in (NEUTRAL_VERSION, CONTEXT_VERSION, VERSION) and stage in ("plan", "plan-revise"):
            from .planner_ending_descriptions import describe as describe_planner_ending

            describe_planner_ending(schema)
        if version in (CONTEXT_VERSION, VERSION):
            from .ending_context_descriptions import describe as describe_ending_context

            describe_ending_context(schema, stage)
        if version == VERSION:
            from .compact_editorial_descriptions import describe_planning

            describe_planning(schema, stage)
        return
    if stage in ("plan", "plan-revise"):
        ending = schema["$defs"]["PlanningFinalMoment"]["properties"]
        ending["last_moment"]["description"] = (
            "The chosen closing passage and what it leaves the listener with. It may complete the "
            "central action or answer, offer a supported reflection, return to an earlier element, "
            "widen the perspective, or leave a meaningful open question. Choose what this story "
            "earns; describe its content and function rather than prescribing spoken lines."
        )
        ending["why_end_here"]["description"] = (
            "Why the story reaches its ending here: how this passage resolves or deliberately leaves "
            "open the central promise, and the concrete understanding or experience that remains "
            "with the listener. The resolution itself "
            "can be the ending; an additional detail or new discovery is not required."
        )
    elif stage.startswith("plan-review"):
        props = schema["properties"]
        props["listener_answer"]["description"] = (
            "Follow this selected central story as a newcomer: the premises available before the "
            "opening question, developments and first disclosures, chronology of actions and "
            "knowledge, and how the ending fulfills or deliberately leaves open the central promise. "
            "Assess the chosen closing passage by its meaning in this story, including a supported "
            "reflection or conclusion when it earns its place. Identify repeated work and material "
            "that displaces the story's resolution."
        )
        props["findings"]["description"] = (
            "Addressed problems in the selected story, novice prerequisites, opening promise and "
            "its resolution, chronological implications, overlapping branches or chosen ending. "
            "Judge whether the closing passage completes this telling; neither an extra final "
            "detail nor a particular ending technique is required. Empty is valid when the plan works."
        )
