from __future__ import annotations

from app.config import settings
from app.generation_v2.contracts.agents import V2AgentName, V2AgentPrefix
from app.generation_v2.contracts.pipeline import V2Phase


def _configured_model(value: str | None) -> str | None:
    model = str(value or "").strip()
    return model or None


def generation_v2_default_model() -> str:
    return (
        _configured_model(settings.generation_v2_default_model)
        or _configured_model(settings.generation_v2_openai_model)
        or "gpt-5.4-mini"
    )


def generation_v2_model_for_agent(phase: str, agent_name: str, *, default_model: str | None = None) -> str:
    fallback = default_model or generation_v2_default_model()
    phase_value = str(phase)
    agent_value = str(agent_name)
    by_agent = {
        V2AgentName.SOURCE_TRIAGE.value: settings.generation_v2_source_triage_model,
        V2AgentName.SOURCE_TRIAGE_PLANNER.value: settings.generation_v2_source_triage_model,
        V2AgentName.SOURCE_TRIAGE_FINALIZER.value: settings.generation_v2_source_triage_model,
        V2AgentName.SOURCE_ACQUISITION.value: settings.generation_v2_source_acquisition_model,
        V2AgentName.RESEARCH_PLANNER.value: settings.generation_v2_research_planner_model,
        V2AgentName.RESEARCH_FINALIZER.value: settings.generation_v2_research_finalizer_model,
        V2AgentName.RESEARCH_AUDITOR.value: settings.generation_v2_research_audit_model,
        V2AgentName.STORY_EDITOR.value: settings.generation_v2_treatment_model,
        V2AgentName.EPISODE_CRITIC.value: settings.generation_v2_critic_model,
        V2AgentName.CITATION_LINKER.value: settings.generation_v2_citation_model,
        V2AgentName.SUPPORT_JUDGE.value: settings.generation_v2_support_judge_model,
    }
    if agent_value.startswith(V2AgentPrefix.SOURCE_ACQUISITION_ROUND.value):
        return _configured_model(settings.generation_v2_source_acquisition_model) or fallback
    if agent_value.startswith(V2AgentPrefix.RESEARCH_JOB.value):
        return (
            _configured_model(settings.generation_v2_research_job_model)
            or _configured_model(settings.generation_v2_research_model)
            or fallback
        )
    if agent_value.startswith(V2AgentPrefix.ACT_WRITER.value):
        return _configured_model(settings.generation_v2_writer_model) or fallback
    if agent_value.startswith(V2AgentPrefix.ACT_REWRITER.value):
        return _configured_model(settings.generation_v2_rewriter_model) or fallback
    phase_fallback = {
        V2Phase.SOURCE_TRIAGE.value: settings.generation_v2_source_triage_model,
        V2Phase.SOURCE_ACQUISITION.value: settings.generation_v2_source_acquisition_model,
        V2Phase.EDITORIAL_RESEARCH.value: settings.generation_v2_research_model,
        V2Phase.RESEARCH_AUDIT.value: settings.generation_v2_research_audit_model,
        V2Phase.TREATMENT.value: settings.generation_v2_treatment_model,
        V2Phase.ACT_DRAFTING.value: settings.generation_v2_writer_model,
        V2Phase.CRITIQUE.value: settings.generation_v2_critic_model,
        V2Phase.CRITIQUE_REWRITE.value: settings.generation_v2_rewriter_model,
        V2Phase.FACT_CITATION.value: settings.generation_v2_citation_model,
        V2Phase.SUPPORT_JUDGMENT.value: settings.generation_v2_support_judge_model,
        V2Phase.CORPUS_PREPARE.value: settings.generation_v2_corpus_prepare_model,
    }
    return (
        _configured_model(by_agent.get(agent_value))
        or _configured_model(phase_fallback.get(phase_value))
        or generation_v2_default_model()
        or fallback
    )


def generation_v2_model_read_model() -> str:
    return _configured_model(settings.generation_v2_model_read_model) or generation_v2_default_model()


def generation_v2_corpus_prepare_model() -> str:
    return _configured_model(settings.generation_v2_corpus_prepare_model) or generation_v2_default_model()
