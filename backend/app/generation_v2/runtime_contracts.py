from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.generation_v2.contracts.agents import V2AgentName


class V2RuntimeUnavailable(RuntimeError):
    pass


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolPolicy:
    file_search: bool = True
    web_search: bool = False
    local_tools: bool = True
    max_file_search_results: int = 8


@dataclass(frozen=True)
class AgentCheckpointPolicy:
    enabled: bool = False
    max_chain_rounds: int = 8
    max_chain_input_tokens: int = 60_000
    max_chain_model_tool_output_chars: int = 80_000


def tool_round_limit(max_tool_rounds: int | None) -> int | None:
    configured = max_tool_rounds if max_tool_rounds is not None else settings.generation_v2_max_tool_rounds
    if configured is None:
        return None
    configured_int = int(configured)
    if configured_int <= 0:
        return None
    return max(1, configured_int)


def default_checkpoint_policy(agent_name: str, *, phase: str | None = None) -> AgentCheckpointPolicy:
    is_triage_agent = agent_name == V2AgentName.SOURCE_TRIAGE or agent_name.startswith("source_triage_")
    is_editorial_research_agent = (
        agent_name == V2AgentName.RESEARCH_PLANNER
        or agent_name == V2AgentName.RESEARCH_FINALIZER
        or agent_name.startswith("research_job_agent_")
    )
    if phase is not None:
        if phase == "v2_source_triage" and is_triage_agent:
            return AgentCheckpointPolicy(enabled=True)
        if phase == "v2_editorial_research" and is_editorial_research_agent:
            return AgentCheckpointPolicy(enabled=True)
        return AgentCheckpointPolicy(enabled=False)
    if is_triage_agent or is_editorial_research_agent:
        return AgentCheckpointPolicy(enabled=True)
    return AgentCheckpointPolicy(enabled=False)
