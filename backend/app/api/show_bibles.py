import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DB
from app.models.project import Project
from app.models.show_bible import ShowBible
from app.observability import push_trace_context, safe_set_run, trace_span, tracing_scope
from app.schemas.show_bible import ShowBibleCreate, ShowBibleResponse, ShowBibleGenerateRequest

router = APIRouter(prefix="/api", tags=["show-bibles"])


PRESETS = [
    {
        "preset_name": "Investigative Duo",
        "show_style": "Investigative",
        "tone_and_mood": "investigative",
        "audience_profile": {"level": "casual", "knows": [], "explain": []},
        "hosts": [
            {"name": "Host", "role": "Guide", "vocabulary_rules": "clear, no jargon without explanation", "interaction_style": "leads narrative, summarizes", "explanation_behavior": "analogies from everyday life", "constraints": []},
            {"name": "Analyst", "role": "Challenger", "vocabulary_rules": "professional terms with explanation", "interaction_style": "asks tough questions, demands proof", "explanation_behavior": "through numbers and comparisons", "constraints": []},
        ],
        "forbidden_phrases": ["In conclusion", "As we all know", "Without further ado", "Let's dive in"],
        "rigor_policy": "Key FACTs in main line must be VERIFIED. OBSERVED acceptable for edge cases.",
        "adjacency_policy": "Adjacent expansion only if: strengthens core mechanism understanding, illustrates conflict/case, or provides necessary context.",
        "explanation_policy": "Each new term: definition → analogy → example. Numbers: intuitive translation.",
        "format_rules": {"max_reply_tokens": 120, "summary_every_n_scenes": 4, "humor_level": "minimal", "profanity_allowed": False},
    },
    {
        "preset_name": "Casual Explainer",
        "show_style": "Conversational",
        "tone_and_mood": "light",
        "audience_profile": {"level": "beginner", "knows": [], "explain": []},
        "hosts": [
            {"name": "Host", "role": "Guide", "vocabulary_rules": "simple everyday language", "interaction_style": "friendly, uses humor", "explanation_behavior": "step-by-step with examples", "constraints": []},
            {"name": "Co-host", "role": "Challenger", "vocabulary_rules": "asks 'dumb' questions audience would ask", "interaction_style": "curious, playful skepticism", "explanation_behavior": "relates to pop culture", "constraints": []},
        ],
        "forbidden_phrases": ["Obviously", "It goes without saying", "As experts agree"],
        "rigor_policy": "FACTs should be VERIFIED or OBSERVED with hedging.",
        "adjacency_policy": "Fun tangents allowed if they help understanding. Max 2 per act.",
        "explanation_policy": "ELI5 approach. Every concept gets a real-world analogy.",
        "format_rules": {"max_reply_tokens": 80, "summary_every_n_scenes": 2, "humor_level": "medium", "profanity_allowed": False},
    },
    {
        "preset_name": "Academic Debate",
        "show_style": "Academic Debate",
        "tone_and_mood": "serious",
        "audience_profile": {"level": "professional", "knows": [], "explain": []},
        "hosts": [
            {"name": "Professor", "role": "Guide", "vocabulary_rules": "academic but accessible", "interaction_style": "structured argumentation", "explanation_behavior": "through evidence and citations", "constraints": []},
            {"name": "Critic", "role": "Challenger", "vocabulary_rules": "technical, precise", "interaction_style": "steel-mans counterarguments", "explanation_behavior": "through counterexamples", "constraints": []},
        ],
        "forbidden_phrases": ["Everyone knows", "It's common sense", "Needless to say"],
        "rigor_policy": "ALL FACTs must be VERIFIED. No UNVERIFIED claims in main line.",
        "adjacency_policy": "Only methodology-adjacent expansions. No tangents.",
        "explanation_policy": "Assume domain knowledge. Define only cross-domain terms.",
        "format_rules": {"max_reply_tokens": 200, "summary_every_n_scenes": 5, "humor_level": "minimal", "profanity_allowed": False},
    },
]


@router.get("/show-bibles/presets", response_model=list[ShowBibleResponse])
async def list_presets(db: DB):
    result = await db.execute(select(ShowBible).where(ShowBible.is_preset.is_(True)))
    presets = result.scalars().all()
    if not presets:
        # Seed presets on first access
        for preset_data in PRESETS:
            preset = ShowBible(
                project_id=None,
                is_preset=True,
                **preset_data,
            )
            db.add(preset)
        await db.flush()
        result = await db.execute(select(ShowBible).where(ShowBible.is_preset.is_(True)))
        presets = result.scalars().all()
    return [ShowBibleResponse.model_validate(p) for p in presets]


@router.post("/projects/{project_id}/show-bible", response_model=ShowBibleResponse, status_code=status.HTTP_201_CREATED)
async def create_or_update_show_bible(project_id: uuid.UUID, data: ShowBibleCreate, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)

    # Check for existing project-level (non-preset, non-run-scoped) ShowBible
    result = await db.execute(
        select(ShowBible).where(
            ShowBible.project_id == project_id,
            ShowBible.is_preset.is_(False),
            ShowBible.generation_run_id.is_(None),
        )
    )
    existing = result.scalar_one_or_none()

    dump = data.model_dump(exclude_unset=True)
    if "audience_profile" in dump and dump["audience_profile"]:
        dump["audience_profile"] = dump["audience_profile"] if isinstance(dump["audience_profile"], dict) else dump["audience_profile"].model_dump() if hasattr(dump["audience_profile"], "model_dump") else dump["audience_profile"]
    if "format_rules" in dump and dump["format_rules"]:
        dump["format_rules"] = dump["format_rules"] if isinstance(dump["format_rules"], dict) else dump["format_rules"].model_dump() if hasattr(dump["format_rules"], "model_dump") else dump["format_rules"]
    if "hosts" in dump:
        dump["hosts"] = [h if isinstance(h, dict) else h.model_dump() if hasattr(h, "model_dump") else h for h in dump["hosts"]]
    if "voice_profiles" in dump:
        dump["voice_profiles"] = [v if isinstance(v, dict) else v.model_dump() if hasattr(v, "model_dump") else v for v in dump["voice_profiles"]]

    if existing:
        for key, value in dump.items():
            setattr(existing, key, value)
        await db.flush()
        await db.refresh(existing)
        return ShowBibleResponse.model_validate(existing)

    bible = ShowBible(project_id=project_id, **dump)
    db.add(bible)
    await db.flush()
    await db.refresh(bible)
    return ShowBibleResponse.model_validate(bible)


@router.get("/projects/{project_id}/show-bible", response_model=ShowBibleResponse)
async def get_show_bible(project_id: uuid.UUID, user: CurrentUser, db: DB):
    await _verify_project_ownership(project_id, user.id, db)
    result = await db.execute(
        select(ShowBible).where(
            ShowBible.project_id == project_id,
            ShowBible.is_preset.is_(False),
            ShowBible.generation_run_id.is_(None),
        )
    )
    bible = result.scalar_one_or_none()
    if bible is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ShowBible not found")
    return ShowBibleResponse.model_validate(bible)


@router.post("/projects/{project_id}/show-bible/generate", response_model=ShowBibleResponse)
async def generate_show_bible(project_id: uuid.UUID, data: ShowBibleGenerateRequest, user: CurrentUser, db: DB):
    trace_ctx = {
        "component": "api.show_bible_generate",
        "project_id": str(project_id),
        "user_id": str(user.id),
    }
    with push_trace_context(trace_ctx):
        with tracing_scope(metadata=trace_ctx, tags=["api", "show-bible"]):
            with trace_span(
                "api.generate_show_bible",
                run_type="chain",
                inputs={
                    "project_id": str(project_id),
                    "topic_chars": len(data.topic),
                    "audience_level": data.audience_level,
                    "show_style": data.show_style,
                },
                tags=["api-endpoint"],
            ) as api_run:
                project = await _get_owned_project(project_id, user.id, db)

                from app.pipeline.phase0_setup.show_configurator import generate_show_bible, ShowBibleGenerationError

                try:
                    bible_data = await generate_show_bible(
                        data.topic,
                        data.audience_level,
                        data.show_style,
                        language=project.language,
                    )
                except ShowBibleGenerationError as exc:
                    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

                result = await db.execute(
                    select(ShowBible).where(
                        ShowBible.project_id == project_id,
                        ShowBible.is_preset.is_(False),
                        ShowBible.generation_run_id.is_(None),
                    )
                )
                existing = result.scalar_one_or_none()

                if existing:
                    bible_data["voice_profiles"] = _merge_generated_voice_profiles(
                        existing.voice_profiles or [],
                        bible_data.get("voice_profiles") or [],
                    )
                    for key, value in bible_data.items():
                        setattr(existing, key, value)
                    await db.flush()
                    await db.refresh(existing)
                    safe_set_run(
                        api_run,
                        outputs={
                            "operation": "update",
                            "show_bible_id": str(existing.id),
                        },
                    )
                    return ShowBibleResponse.model_validate(existing)

                bible = ShowBible(project_id=project_id, **bible_data)
                db.add(bible)
                await db.flush()
                await db.refresh(bible)
                safe_set_run(
                    api_run,
                    outputs={
                        "operation": "create",
                        "show_bible_id": str(bible.id),
                    },
                )
                return ShowBibleResponse.model_validate(bible)


def _merge_generated_voice_profiles(current_profiles: list, generated_profiles: list) -> list:
    """Keep user-selected voices when AI regenerates textual ShowBible fields."""
    merged: list[dict] = []

    for idx in range(2):
        generated = generated_profiles[idx] if idx < len(generated_profiles) and isinstance(generated_profiles[idx], dict) else {}
        current = current_profiles[idx] if idx < len(current_profiles) and isinstance(current_profiles[idx], dict) else {}

        profile = dict(generated)
        profile.setdefault("host_index", idx)
        profile.setdefault("tts_provider", "elevenlabs")
        profile.setdefault("voice_id", "")
        profile.setdefault("pace", "medium")
        profile.setdefault("pitch", "medium")

        current_voice_id = current.get("voice_id")
        if isinstance(current_voice_id, str) and current_voice_id.strip():
            profile["voice_id"] = current_voice_id.strip()

        current_settings = current.get("settings")
        if isinstance(current_settings, dict) and "settings" not in profile:
            profile["settings"] = current_settings

        current_host_name = current.get("host_name")
        if isinstance(current_host_name, str) and current_host_name.strip() and "host_name" not in profile:
            profile["host_name"] = current_host_name.strip()

        merged.append(profile)

    return merged


async def _get_owned_project(project_id: uuid.UUID, user_id: uuid.UUID, db: DB) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id, Project.user_id == user_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _verify_project_ownership(project_id: uuid.UUID, user_id: uuid.UUID, db):
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
