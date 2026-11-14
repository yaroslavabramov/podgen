"""Translate the user's topic into ordinary role data, without prescribing an answer."""

from uuid import uuid5

from podgen.research.hosted.contracts import ResearchConfig
from podgen.writing.editorial_contract import Assignment


def assignment(config):
    return Assignment(
        key=str(config.run_id),
        title=config.topic,
        question=config.topic,
        audience=config.audience,
        listener_outcome=config.topic,
        format=config.format,
        output_language=config.output_language,
        speakers=[dict(label=config.speaker_label, function="Рассказчик", speech_register="Разговорный")],
        duration=dict(
            target_seconds=config.target_seconds,
            min_seconds=config.min_seconds,
            max_seconds=config.max_seconds,
        ),
        knowledge_cutoff=config.knowledge_cutoff,
        required_topics=[],
        excluded_topics=[],
        style_guidance=[],
    )


def initial_research(config):
    return ResearchConfig(
        run_id=uuid5(config.run_id, "initial-research/1"),
        instructions=config.settings.research_instructions,
        profile=config.settings.research_profile,
        assignment=dict(
            user_request=config.topic,
            main_question=config.topic,
            output_language=config.output_language,
            audience=config.audience,
            episode=dict(
                format=config.format,
                target_seconds=config.target_seconds,
                min_seconds=config.min_seconds,
                max_seconds=config.max_seconds,
            ),
            scope=dict(source_mode="open_web"),
        ),
    )
