"""Phase 0: Show Setup -- generate or load ShowBible."""

import json
import logging
import uuid

from sqlalchemy import select

from app.database import async_session_factory
from app.models.show_bible import ShowBible
from app.models.generation_run import GenerationRun
from app.models.project import Project
from app.pipeline.state import PipelineState
from app.services.llm import call_sonnet_structured
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, phase_completed, step_started, step_completed

import time

logger = logging.getLogger(__name__)

SHOW_BIBLE_SYSTEM = """You are a podcast show designer. Given the topic, audience level, and show style,
generate a complete ShowBible configuration for a 2-host podcast.

Return valid JSON with this exact schema:
{
  "audience_profile": {
    "level": "<beginner|casual|professional>",
    "assumed_knowledge": ["list of things audience already knows"],
    "needs_explanation": ["concepts that must be explained"],
    "vocabulary_ceiling": "<description of max complexity>"
  },
  "tone_and_mood": "<serious|light|comedic|investigative>",
  "hosts": [
    {
      "name": "<host name>",
      "role": "Guide",
      "vocabulary_rules": "<description>",
      "interaction_style": "<description>",
      "explanation_behavior": "<description>",
      "constraints": "<description>"
    },
    {
      "name": "<host name>",
      "role": "Challenger",
      "vocabulary_rules": "<description>",
      "interaction_style": "<description>",
      "explanation_behavior": "<description>",
      "constraints": "<description>"
    }
  ],
  "voice_profiles": [
    {"host_index": 0, "tts_provider": "elevenlabs", "voice_id": "", "pace": "medium", "pitch": "medium"},
    {"host_index": 1, "tts_provider": "elevenlabs", "voice_id": "", "pace": "medium", "pitch": "medium"}
  ],
  "forbidden_phrases": ["list of LLM cliches to avoid"],
  "rigor_policy": "<description of fact-checking rigor requirements>",
  "adjacency_policy": "<rules for tangential expansion>",
  "explanation_policy": "<how to explain complex concepts for this audience>",
  "format_rules": {
    "max_reply_tokens": 150,
    "summary_frequency": "every_2_topics",
    "humor_allowed": true,
    "tts_tags_enabled": true
  }
}"""


async def generate_show_bible(topic: str, audience_level: str, show_style: str) -> dict:
    """Use Claude Sonnet to generate ShowBible fields from topic parameters."""
    prompt = json.dumps({
        "task": "generate_show_bible",
        "topic": topic,
        "audience_level": audience_level,
        "show_style": show_style,
        "instructions": (
            "Design a 2-host podcast show bible for the given topic. "
            "The Guide host explains and structures; the Challenger asks tough questions and demands proof. "
            "Tailor vocabulary, explanation depth, and rigor to the audience level. "
            "Make forbidden_phrases comprehensive -- block all common LLM filler."
        ),
    })

    raw = await call_sonnet_structured(
        system=SHOW_BIBLE_SYSTEM,
        prompt=prompt,
        max_tokens=4096,
        temperature=0.7,
    )
    return json.loads(raw)


async def setup_show(state: PipelineState) -> dict:
    """LangGraph node: ensure a ShowBible exists for this generation run.

    If show_bible_id is already set, validates it exists. Otherwise generates
    a new ShowBible using LLM and persists it.

    Returns partial state update with show_bible_id.
    """
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, phase_started("phase0_setup", "Setting up show configuration"))
    update_generation_progress(run_id, "phase0_setup", "show_configurator", 0.0)

    async with async_session_factory() as db:
        # Load the generation run to get config and existing show_bible_id
        result = await db.execute(
            select(GenerationRun).where(GenerationRun.id == uuid.UUID(run_id))
        )
        run = result.scalar_one()

        # If show_bible_id already provided, verify it exists
        if state.get("show_bible_id"):
            publish_event(run_id, step_started("phase0_setup", "validate_bible", "Validating existing ShowBible"))
            result = await db.execute(
                select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"]))
            )
            bible = result.scalar_one_or_none()
            if bible is not None:
                publish_event(run_id, step_completed("phase0_setup", "validate_bible"))
                duration_ms = int((time.time() - t0) * 1000)
                publish_event(run_id, phase_completed("phase0_setup", duration_ms))
                update_generation_progress(run_id, "phase0_setup", "complete", 5.0)
                return {"show_bible_id": str(bible.id), "current_phase": "phase0_setup"}

        # Generate a new ShowBible
        publish_event(run_id, step_started("phase0_setup", "generate_bible", "Generating ShowBible with AI"))

        # Extract parameters from run config or state
        config = run.config or {}
        topic = state.get("user_prompt", config.get("topic", "general"))
        audience_level = config.get("audience_level", "casual")
        show_style = config.get("show_style", "Conversational")

        bible_data = await generate_show_bible(topic, audience_level, show_style)

        # Persist the ShowBible
        bible = ShowBible(
            project_id=uuid.UUID(state["project_id"]),
            audience_profile=bible_data.get("audience_profile", {}),
            tone_and_mood=bible_data.get("tone_and_mood", "conversational"),
            show_style=show_style,
            hosts=bible_data.get("hosts", []),
            voice_profiles=bible_data.get("voice_profiles", []),
            forbidden_phrases=bible_data.get("forbidden_phrases", []),
            rigor_policy=bible_data.get("rigor_policy"),
            adjacency_policy=bible_data.get("adjacency_policy"),
            explanation_policy=bible_data.get("explanation_policy"),
            format_rules=bible_data.get("format_rules", {}),
        )
        db.add(bible)
        await db.flush()

        # Update the generation run to reference this bible
        run.show_bible_id = bible.id
        await db.commit()

        bible_id = str(bible.id)

    publish_event(run_id, step_completed("phase0_setup", "generate_bible"))
    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase0_setup", duration_ms))
    update_generation_progress(run_id, "phase0_setup", "complete", 5.0)

    return {"show_bible_id": bible_id, "current_phase": "phase0_setup"}
