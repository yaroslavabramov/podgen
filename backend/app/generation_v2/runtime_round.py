from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.runtime_contracts import AgentTool, ToolPolicy
from app.generation_v2.runtime_checkpoint_state import commit_runtime_side_effects as _commit_runtime_side_effects
from app.generation_v2.runtime_response import (
    file_search_calls as _file_search_calls,
    function_calls as _function_calls,
    hosted_evidence_followup_input as _hosted_evidence_followup_input,
    web_search_calls as _web_search_calls,
)
from app.generation_v2.runtime_response_create import create_runtime_response as _create_runtime_response
from app.generation_v2.runtime_round_trace import record_round_response_trace as _record_round_response_trace
from app.pipeline.step_control import assert_step_not_stopped


@dataclass
class RuntimeRoundState:
    round_idx: int
    response: Any
    file_search_calls: list[dict[str, Any]]
    web_search_calls: list[dict[str, Any]]
    function_calls: list[Any]
    usage: dict[str, Any]
    hosted_followup: dict[str, Any] | None = None


class RuntimeRoundRunner:
    def __init__(
        self,
        *,
        runtime: Any,
        client: Any,
        db: AsyncSession,
        generation_run_id: Any,
        phase: str,
        agent_name: str,
        prompt_hash: str,
        system: str,
        prompt: str,
        schema_model: type[BaseModel],
        schema: dict[str, Any],
        tools: list[dict[str, Any]],
        policy: ToolPolicy,
        requested_model: str,
        vector_store_id: str | None,
        local_tools: list[AgentTool] | None,
        hosted_file_search_handler: Callable[[list[dict[str, Any]], str, str, str], Awaitable[dict[str, Any] | None]]
        | None,
        max_output_tokens: int | None,
        server_compaction_threshold: int | None,
        temperature: float | None,
    ) -> None:
        self.runtime = runtime
        self.client = client
        self.db = db
        self.generation_run_id = generation_run_id
        self.phase = phase
        self.agent_name = agent_name
        self.prompt_hash = prompt_hash
        self.system = system
        self.prompt = prompt
        self.schema_model = schema_model
        self.schema = schema
        self.tools = tools
        self.policy = policy
        self.requested_model = requested_model
        self.vector_store_id = vector_store_id
        self.local_tools = local_tools
        self.hosted_file_search_handler = hosted_file_search_handler
        self.max_output_tokens = max_output_tokens
        self.server_compaction_threshold = server_compaction_threshold
        self.temperature = temperature
        self.announced_hosted_evidence_ids: set[str] = set()

    async def run_round(
        self,
        *,
        round_idx: int,
        input_payload: Any,
        previous_response_id: str | None,
        chain_id: str,
        checkpoint_index: int,
        chain_round: int,
    ) -> RuntimeRoundState:
        await assert_step_not_stopped()
        response_create = await _create_runtime_response(
            client=self.client,
            generation_run_id=self.generation_run_id,
            phase=self.phase,
            agent_name=self.agent_name,
            round_number=round_idx + 1,
            prompt_hash=self.prompt_hash,
            system=self.system,
            prompt=self.prompt,
            schema_name=self.schema_model.__name__,
            schema=self.schema,
            tools=self.tools,
            policy=self.policy,
            input_payload=input_payload,
            previous_response_id=previous_response_id,
            requested_model=self.requested_model,
            reasoning_effort=self.runtime.reasoning_effort,
            vector_store_id=self.vector_store_id,
            local_tools=self.local_tools,
            max_output_tokens=self.max_output_tokens,
            server_compaction_threshold=self.server_compaction_threshold,
            temperature=self.temperature,
            chain_id=chain_id,
            checkpoint_index=checkpoint_index,
            chain_round=chain_round,
        )
        response = response_create.response
        file_search_calls = _file_search_calls(response)
        web_search_calls = _web_search_calls(response)
        function_calls = _function_calls(response)
        response_trace = await _record_round_response_trace(
            self.db,
            generation_run_id=self.generation_run_id,
            phase=self.phase,
            agent_name=self.agent_name,
            round_idx=round_idx,
            prompt_hash=self.prompt_hash,
            system=self.system,
            prompt=self.prompt,
            schema_name=self.schema_model.__name__,
            schema=self.schema,
            tools=self.tools,
            policy=self.policy,
            input_payload=input_payload,
            response=response,
            run_tree=response_create.run_tree,
            requested_model=self.requested_model,
            latency_ms=response_create.latency_ms,
            file_search_calls=file_search_calls,
            web_search_calls=web_search_calls,
            function_calls=function_calls,
            chain_id=chain_id,
            checkpoint_index=checkpoint_index,
            chain_round=chain_round,
            previous_response_id=previous_response_id,
            max_output_tokens=self.max_output_tokens,
            server_compaction_threshold=self.server_compaction_threshold,
        )
        await _commit_runtime_side_effects(self.db)
        round_state = RuntimeRoundState(
            round_idx=round_idx,
            response=response,
            file_search_calls=file_search_calls,
            web_search_calls=web_search_calls,
            function_calls=function_calls,
            usage=response_trace.usage,
        )
        round_state.hosted_followup = await self.hosted_followup(round_state)
        return round_state

    async def hosted_followup(self, round_state: RuntimeRoundState) -> dict[str, Any] | None:
        hosted_summary: dict[str, Any] | None = None
        if round_state.file_search_calls and self.hosted_file_search_handler:
            hosted_summary = await self.hosted_file_search_handler(
                round_state.file_search_calls,
                round_state.response.id,
                self.phase,
                self.agent_name,
            )
            await _commit_runtime_side_effects(self.db)
        hosted_followup = _hosted_evidence_followup_input(
            hosted_summary,
            announced_ids=self.announced_hosted_evidence_ids,
            tools=self.local_tools or [],
        )
        if hosted_followup:
            self.announced_hosted_evidence_ids.update(hosted_followup["evidence_ids"])
        return hosted_followup


__all__ = ["RuntimeRoundRunner", "RuntimeRoundState"]
