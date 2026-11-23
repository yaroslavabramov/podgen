"""Existing provider adapters with the frozen profiles' original routing."""

import os

from podgen.domain.errors import ContractError
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.profiles import load_profile


def credentials(repo, project, settings):
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    required = {settings.research_profile.credential_env, settings.audio_profile.credential_env}
    if settings.audio_verification is not None:
        required.add(settings.audio_verification.credential_env)
    for ref, stage in (
        (settings.writer_profile_ref, "draft"),
        (settings.reviewer_profile_ref, "fact-review"),
        (settings.planner_profile_ref, "plan"),
        (settings.plan_reviewer_profile_ref, "plan-review"),
    ):
        profile = load_profile(generation, project, ref, stage)
        required.add({"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}[profile.provider])
    if any(not os.environ.get(key) for key in required):
        raise ContractError("episode_provider_credential_missing")


def model_provider(profile):
    if profile.provider == "anthropic":
        from podgen.runtime.models.anthropic_provider import AnthropicProvider
        from podgen.runtime.models.messages_versions import api_version

        return AnthropicProvider(
            os.environ.get("ANTHROPIC_API_KEY", ""),
            api_version=api_version(profile.adapter_version),
            timeout_seconds=profile.timeout_policy.get("rpc_timeout_seconds", 1800),
        )
    from podgen.runtime.models.openai_provider import OpenAIProvider

    return OpenAIProvider(
        os.environ.get("OPENAI_API_KEY", ""),
        timeout_seconds=profile.timeout_policy.get("rpc_timeout_seconds", 1800),
    )


def research_provider(settings):
    from podgen.research.hosted.provider import GeminiResearchProvider
    from podgen.research.hosted.transport import HttpTransport

    return GeminiResearchProvider(HttpTransport(settings.research_profile.request_timeout_seconds))


def recognition_transport(settings):
    from podgen.audio.http import RawAudioTransport

    verification = settings.audio_verification
    return (
        RawAudioTransport(timeout_seconds=verification.recognition.request_timeout_seconds)
        if verification
        else None
    )


def audio_transport():
    from podgen.audio.synthesis.transport import HttpSynthesisTransport

    return HttpSynthesisTransport(timeout_seconds=900)
