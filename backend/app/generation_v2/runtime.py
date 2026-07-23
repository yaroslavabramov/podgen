from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import openai
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.model_routing import (
    generation_v2_default_model,
    generation_v2_model_for_agent,
)
from app.generation_v2.runtime_contracts import (
    AgentCheckpointPolicy,
    AgentOperationalStall,
    AgentTerminalNotReached,
    AgentTerminalResult,
    AgentTool,
    CheckpointResumeContext,
    ToolPolicy,
    V2RuntimeUnavailable,
    default_checkpoint_policy as _default_checkpoint_policy,
    tool_round_limit as _tool_round_limit,
)
from app.generation_v2.runtime_checkpoint import (
    CHECKPOINT_LIST_MAX_ITEMS,
    CHECKPOINT_STRING_MAX_CHARS,
    MODEL_TOOL_MAX_DEPTH,
    collect_tool_ids,
    compact_checkpoint_value,
)
from app.generation_v2.runtime_checkpoint_state import (
    build_agent_checkpoint_state as _build_agent_checkpoint_state,
    checkpoint_restart_message as _checkpoint_restart_message,
    checkpoint_source_inventory as _checkpoint_source_inventory,
    checkpoint_trim as _checkpoint_trim,
)
from app.generation_v2.runtime_debug import (
    runtime_debug_payload as _runtime_debug_payload,
)
from app.generation_v2.runtime_response import (
    file_search_calls as _file_search_calls,
    function_calls as _function_calls,
    has_tool as _has_tool,
    hosted_evidence_followup_input as _hosted_evidence_followup_input,
    item_get as _item_get,
    to_plain_dict as _to_plain_dict,
    web_search_calls as _web_search_calls,
)
from app.generation_v2.runtime_call import call_structured_runtime as _call_structured_runtime
from app.generation_v2.runtime_parse import parse_structured_runtime_response as _parse_structured_runtime_response
from app.generation_v2.runtime_response_trace import (
    runtime_response_trace_payload as _runtime_response_trace_payload,
    save_response_create_trace as _save_response_create_trace,
    set_response_run_outputs as _set_response_run_outputs,
)
from app.generation_v2.runtime_schema import (
    strict_json_schema as _strict_json_schema,
    strict_object as _strict_object,
)
from app.generation_v2.runtime_tool_specs import runtime_tool_specs as _runtime_tool_specs
from app.generation_v2.runtime_tool_output import (
    MODEL_HIDDEN_TOOL_KEYS,
    MODEL_FACING_CONTRACT_VERSION,
    ToolOutputContractViolation,
    model_facing_tool_output,
    tool_model_facing_schema,
)
from app.generation_v2.runtime_tool_calls import process_runtime_function_calls as _process_runtime_function_calls
from app.services.llm import get_openai_client

T = TypeVar("T", bound=BaseModel)


_CHECKPOINT_LAST_TOOL_RESULT_LIMIT = 12
_CHECKPOINT_STATE_ID_LIMIT = 40

_CHECKPOINT_STRING_MAX_CHARS = CHECKPOINT_STRING_MAX_CHARS
_CHECKPOINT_LIST_MAX_ITEMS = CHECKPOINT_LIST_MAX_ITEMS
_MODEL_TOOL_MAX_DEPTH = MODEL_TOOL_MAX_DEPTH
_MODEL_FACING_CONTRACT_VERSION = MODEL_FACING_CONTRACT_VERSION
_MODEL_HIDDEN_TOOL_KEYS = MODEL_HIDDEN_TOOL_KEYS


class V2Runtime:
    """OpenAI-first structured runtime with hosted and local tools."""

    def __init__(
        self,
        *,
        before_provider_call: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.default_model = generation_v2_default_model()
        self.model = self.default_model
        self.reasoning_effort = settings.generation_v2_openai_reasoning_effort
        self._prompt_hashes: dict[tuple[str, str], str] = {}
        self._fallback_agents: set[tuple[str, str]] = set()
        self.before_provider_call = before_provider_call

    def model_for(self, phase: str, agent_name: str) -> str:
        return generation_v2_model_for_agent(phase, agent_name, default_model=self.default_model)

    def prompt_hash_for(self, phase: str, agent_name: str) -> str | None:
        return self._prompt_hashes.get((phase, agent_name))

    @property
    def fallback_used(self) -> bool:
        return bool(self._fallback_agents)

    def mark_fallback_used(self, phase: str, agent_name: str) -> None:
        self._fallback_agents.add((phase, agent_name))

    def fallback_metadata_for(self, phase: str, agent_name: str) -> dict[str, Any] | None:
        if (phase, agent_name) not in self._fallback_agents:
            return None
        return {"runtime_fallback": True, "fallback_phase": phase, "fallback_agent": agent_name}

    async def call_structured(
        self,
        *,
        db: AsyncSession,
        generation_run_id,
        phase: str,
        agent_name: str,
        schema_model: type[T],
        system: str,
        prompt: str,
        vector_store_id: str | None = None,
        local_tools: list[AgentTool] | None = None,
        tool_policy: ToolPolicy | None = None,
        hosted_file_search_handler: Callable[[list[dict[str, Any]], str, str, str], Awaitable[dict[str, Any] | None]]
        | None = None,
        max_output_tokens: int | None = None,
        max_tool_rounds: int | None = None,
        server_compaction_threshold: int | None = None,
        temperature: float | None = None,
        checkpoint_policy: AgentCheckpointPolicy | None = None,
        checkpoint_resume_context: CheckpointResumeContext | None = None,
        terminal_progress_loader: Callable[[], Awaitable[Any]] | None = None,
    ) -> T:
        return await _call_structured_runtime(
            self,
            db=db,
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            schema_model=schema_model,
            system=system,
            prompt=prompt,
            vector_store_id=vector_store_id,
            local_tools=local_tools,
            tool_policy=tool_policy,
            hosted_file_search_handler=hosted_file_search_handler,
            max_output_tokens=max_output_tokens,
            max_tool_rounds=max_tool_rounds,
            server_compaction_threshold=server_compaction_threshold,
            temperature=temperature,
            checkpoint_policy=checkpoint_policy,
            checkpoint_resume_context=checkpoint_resume_context,
            terminal_progress_loader=terminal_progress_loader,
        )

    def _tool_specs(
        self,
        *,
        vector_store_id: str | None,
        local_tools: list[AgentTool],
        policy: ToolPolicy,
    ) -> list[dict[str, Any]]:
        return _runtime_tool_specs(vector_store_id=vector_store_id, local_tools=local_tools, policy=policy)

    def _get_openai_client(self):
        return get_openai_client()

    async def _process_function_calls(self, db: AsyncSession, **kwargs):
        return await _process_runtime_function_calls(db, **kwargs)


_model_facing_tool_output = model_facing_tool_output
_tool_model_facing_schema = tool_model_facing_schema
_compact_checkpoint_value = compact_checkpoint_value
_collect_tool_ids = collect_tool_ids

__all__ = [
    "_has_tool",
    "_item_get",
    "_to_plain_dict",
    "_strict_object",
    "_file_search_calls",
    "_function_calls",
    "_web_search_calls",
    "_hosted_evidence_followup_input",
    "_runtime_debug_payload",
    "_runtime_response_trace_payload",
    "_save_response_create_trace",
    "_set_response_run_outputs",
    "_parse_structured_runtime_response",
    "_process_runtime_function_calls",
    "_strict_json_schema",
    "AgentTerminalResult",
    "AgentTerminalNotReached",
    "AgentOperationalStall",
    "AgentTool",
    "AgentCheckpointPolicy",
    "CheckpointResumeContext",
    "ToolPolicy",
    "V2Runtime",
    "V2RuntimeUnavailable",
    "_default_checkpoint_policy",
    "_tool_round_limit",
    "get_openai_client",
    "_build_agent_checkpoint_state",
    "_checkpoint_restart_message",
    "_checkpoint_source_inventory",
    "_checkpoint_trim",
    "save_tool_trace",
    "openai",
    "ToolOutputContractViolation",
]
