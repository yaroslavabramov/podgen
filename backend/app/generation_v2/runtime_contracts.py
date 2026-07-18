from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.generation_v2.contracts.agents import V2AgentName


class V2RuntimeUnavailable(RuntimeError):
    pass


class AgentTerminalNotReached(RuntimeError):
    """The model returned normally without completing a required terminal tool."""

    def __init__(self, message: str, *, repeated_without_progress: bool = False) -> None:
        super().__init__(message)
        self.repeated_without_progress = repeated_without_progress


class AgentOperationalStall(RuntimeError):
    """The agent repeated the same non-terminal tool cycle without new information."""


@dataclass(frozen=True)
class AgentTerminalResult:
    """Authoritative result produced by an accepted terminal tool."""

    accepted: bool
    completion: Any | None = None


TerminalResultBuilder = Callable[[dict[str, Any], dict[str, Any]], Any]
TerminalResumeLoader = Callable[[], Awaitable[AgentTerminalResult | None]]


@dataclass
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    terminal: bool = False
    terminal_result_builder: TerminalResultBuilder | None = None
    terminal_resume_loader: TerminalResumeLoader | None = None


def terminal_result_from_tool_output(
    tool: AgentTool,
    *,
    arguments: dict[str, Any],
    output: dict[str, Any],
) -> AgentTerminalResult | None:
    """Resolve the terminal contract without making another model call.

    Terminal handlers must explicitly return ``accepted=True``. The completion
    payload either comes from ``terminal_result_builder`` or from the reserved
    ``terminal_result`` output field. Rejected terminal calls remain ordinary
    tool calls so the agent can repair their arguments and retry.
    """

    if not tool.terminal:
        return None
    if output.get("accepted") is not True:
        return AgentTerminalResult(accepted=False)
    completion = (
        tool.terminal_result_builder(arguments, output)
        if tool.terminal_result_builder is not None
        else output.get("terminal_result")
    )
    if completion is None:
        raise RuntimeError(f"Terminal tool {tool.name!r} was accepted without a terminal completion payload")
    return AgentTerminalResult(accepted=True, completion=completion)


@dataclass(frozen=True)
class ToolPolicy:
    file_search: bool = True
    web_search: bool = False
    local_tools: bool = True
    max_file_search_results: int = 8
    parallel_local_tool_calls: bool = False
    # Hosted file search is model-visible before backend evidence validation.
    # It must therefore be pinned at request time, not filtered only after the
    # model has already seen a result from another corpus revision.
    file_search_source_version_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCheckpointPolicy:
    enabled: bool = False
    # A fixed round count is not a context-pressure signal. Restarting a healthy
    # Responses chain after an arbitrary number of tool calls discards useful
    # model state and can make an agent repeat durable work. Keep the knob for
    # explicit tests/overrides, but do not enable it by default.
    max_chain_rounds: int | None = None
    max_chain_input_tokens: int = 60_000
    max_chain_model_tool_output_chars: int = 80_000


@dataclass(frozen=True)
class CheckpointResumeContext:
    """Immutable task contract supplied independently of rendered prompt state."""

    workflow_contract: str
    task_context: dict[str, Any]
    contract_version: str = "v2.checkpoint_resume.v1"


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
