from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from app.config import settings
from app.generation_v2.agent_output_policy import semantic_agent_output_limit
from app.generation_v2.agents.research_agents import (
    build_research_finalizer_agent,
    build_research_job_agent,
    build_research_planner_agent,
)
from app.generation_v2.prompting import (
    RESEARCH_FINALIZER_SYSTEM,
    RESEARCH_JOB_SYSTEM,
    RESEARCH_PLANNER_SYSTEM,
)
from app.generation_v2.schemas import ResearchWorkflowCompletion


_PRE_TREATMENT_AGENT_FILES = [
    "source_agents.py",
    "source_acquisition_agent.py",
    "corpus_brief_audit_agent.py",
    "research_agents.py",
    "audit_agent.py",
    "treatment_agent.py",
]


def test_semantic_agent_output_policy_defaults_to_provider_and_has_one_optional_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "generation_v2_semantic_agent_max_output_tokens", None)
    assert semantic_agent_output_limit() is None

    monkeypatch.setattr(settings, "generation_v2_semantic_agent_max_output_tokens", 0)
    assert semantic_agent_output_limit() is None

    monkeypatch.setattr(settings, "generation_v2_semantic_agent_max_output_tokens", 24_000)
    assert semantic_agent_output_limit() == 24_000


def test_semantic_agents_use_shared_output_policy_except_continuous_triage() -> None:
    agent_dir = Path(__file__).parents[1] / "app" / "generation_v2" / "agents"
    for filename in _PRE_TREATMENT_AGENT_FILES:
        tree = ast.parse((agent_dir / filename).read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call_structured"
        ]
        assert calls, filename
        for call in calls:
            keyword = next(
                (item for item in call.keywords if item.arg == "max_output_tokens"),
                None,
            )
            assert keyword is not None, filename
            if filename == "source_agents.py":
                assert isinstance(keyword.value, ast.Constant), filename
                assert keyword.value.value is None, filename
                continue
            assert isinstance(keyword.value, ast.Call), filename
            assert isinstance(keyword.value.func, ast.Name), filename
            assert keyword.value.func.id == "semantic_agent_output_limit", filename


@pytest.mark.asyncio
async def test_research_agents_receive_distinct_stage_roles() -> None:
    captured: dict[str, str] = {}

    class Runtime:
        async def call_structured(self, **kwargs):
            captured[str(kwargs["agent_name"])] = str(kwargs["system"])
            return ResearchWorkflowCompletion(ready=True, summary="done", notes=[])

    run = SimpleNamespace(
        id=uuid.uuid4(),
        config={"user_prompt": "Topic", "duration_target": 1800, "language": "en"},
    )
    runtime = Runtime()
    await build_research_planner_agent(
        None,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        workspace_summary={},
        source_map_summary={},
    )
    await build_research_job_agent(
        None,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        job_payload={"job": {"job_id": "job-1"}},
    )
    await build_research_finalizer_agent(
        None,  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        workspace_summary={},
    )

    assert captured["research_planner_agent"] == RESEARCH_PLANNER_SYSTEM
    assert captured["research_job_agent_job-1"] == RESEARCH_JOB_SYSTEM
    assert captured["research_finalizer_agent"] == RESEARCH_FINALIZER_SYSTEM
    assert len(set(captured.values())) == 3
    assert "Do not read or search sources" in RESEARCH_PLANNER_SYSTEM
    assert "Do not redesign the research plan" in RESEARCH_JOB_SYSTEM
    assert "Do not search or read broadly" in RESEARCH_FINALIZER_SYSTEM
