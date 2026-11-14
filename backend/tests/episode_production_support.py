"""Real journals and services, with local research, source and audio transports."""

from uuid import uuid4

from podgen.episodes.production.contracts import ProductionConfig, ProductionSettings
from podgen.episodes.production.service import EpisodeProduction
from podgen.runtime.evaluation.profiles import create_profile
from podgen.sources.acquisition import service
from tests.audio_cases import config as audio_config
from tests.audio_cases import response as audio_response
from tests.audio_cases import signal
from tests.hosted_research_support import Provider, response
from tests.research_material_provider import ResearchProvider
from tests.source_acquisition_support import Fetcher, setup

REPORT = (
    "# Report\n\n"
    + "Complete connected explanation. " * 50
    + "\n\n**Sources:**\n11. [Document](https://example.com/article)\n"
)


class Speech:
    def __init__(self):
        self.calls = []

    def submit(self, request, credential):
        self.calls.append(request)
        return audio_response(request.provider, signal(seconds=3))


def setup_episode(repo, scope, producer, monkeypatch):
    request, _ = setup(repo, scope, producer)
    monkeypatch.setattr(service, "_check_ssrf", lambda url: None)
    monkeypatch.setenv("TEST_RESEARCH_KEY", "fixture")
    monkeypatch.setenv("TEST_SYNTHESIS_KEY", "fixture")
    refs = {}
    for key, roles in (
        ("writer", ["writer"]),
        ("reviewer", ["editorial_reviewer", "plan_reviewer"]),
        ("planner", ["planner"]),
    ):
        refs[key] = create_profile(
            repo,
            scope[0],
            model={"writer": "claude-opus-5-5", "reviewer": "gpt-6.1-terra", "planner": "gpt-6.1-sol"}[key],
            provider="anthropic" if key == "writer" else "openai",
            context_limit=200000,
            output_limit=24576,
            roles=roles,
            reasoning_effort="medium",
        ).ref
    candidate = audio_config().profiles[0]
    settings = ProductionSettings(
        research_instructions="Research the supplied assignment.",
        research_profile=dict(agent="deep-research-preview-04-2026", credential_env="TEST_RESEARCH_KEY"),
        acquisition=dict(profile_ref=request.profile_ref, rights=request.rights),
        writer_profile_ref=refs["writer"],
        reviewer_profile_ref=refs["reviewer"],
        planner_profile_ref=refs["planner"],
        plan_reviewer_profile_ref=refs["reviewer"],
        audio_profile=candidate,
        writing_limits=dict(
            max_total_input_tokens=196608, max_total_output_tokens=131072, max_elapsed_seconds=7200
        ),
        max_research_jobs=2,
        audio_max_cost_usd="5",
        accepts_variable_research_cost=True,
    )
    config = ProductionConfig(
        run_id=uuid4(), topic="Как расшифровали египетские иероглифы", settings=settings
    )
    fetcher = Fetcher()

    async def acquire(repo, project, request):
        return await service.acquire_source(repo, project, request, fetcher=fetcher)

    hosted = Provider(
        response("completed", steps=[dict(type="model_output", content=[dict(type="text", text=REPORT)])])
    )
    models, speech = ResearchProvider(), Speech()
    runner = EpisodeProduction(
        repo,
        scope[0],
        config.run_id,
        research_provider=hosted,
        model_provider=lambda _: models,
        audio_transport=speech,
        acquire=acquire,
    )
    return config, runner, hosted, models, speech, fetcher
