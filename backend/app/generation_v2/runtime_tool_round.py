from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_contracts import (
    AgentCheckpointPolicy,
    AgentTerminalResult,
    AgentTool,
    CheckpointResumeContext,
    ToolPolicy,
)
from app.generation_v2.runtime_checkpoint_state import (
    checkpoint_reason as _checkpoint_reason,
    checkpoint_restart as _checkpoint_restart,
)
from app.generation_v2.runtime_debug import runtime_debug_payload as _runtime_debug_payload
from app.generation_v2.runtime_hosted_followup import (
    append_hosted_followup_tool_output as _append_hosted_followup_tool_output,
)
from app.generation_v2.runtime_round import RuntimeRoundState


@dataclass(frozen=True)
class RuntimeToolAdvance:
    input_payload: Any
    previous_response_id: str | None
    delivery_receipt_refs: list[str]
    checkpoint_index: int
    chain_id: str
    chain_round: int
    chain_model_tool_output_chars: int
    tool_cycle_fingerprint: str | None = None
    terminal_result: AgentTerminalResult | None = None


class RuntimeToolRoundProcessor:
    def __init__(
        self,
        *,
        runtime: Any,
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
        local_tools: list[AgentTool] | None,
        checkpoint: AgentCheckpointPolicy,
        checkpoint_resume_context: CheckpointResumeContext | None,
    ) -> None:
        self.runtime = runtime
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
        self.local_tools = local_tools
        self.checkpoint = checkpoint
        self.checkpoint_resume_context = checkpoint_resume_context

    async def process(
        self,
        round_state: RuntimeRoundState,
        *,
        input_payload: Any,
        chain_id: str,
        checkpoint_index: int,
        chain_round: int,
        chain_model_tool_output_chars: int,
    ) -> RuntimeToolAdvance:
        tool_call_result = await self.runtime._process_function_calls(
            self.db,
            generation_run_id=self.generation_run_id,
            phase=self.phase,
            agent_name=self.agent_name,
            round_number=round_state.round_idx + 1,
            chain_id=chain_id,
            checkpoint_index=checkpoint_index,
            chain_round=chain_round,
            function_calls=round_state.function_calls,
            local_tools=self.local_tools,
            chain_model_tool_output_chars=chain_model_tool_output_chars,
        )
        tool_outputs = tool_call_result.tool_outputs
        followup_input_messages = tool_call_result.followup_input_messages
        delivery_receipt_refs = tool_call_result.delivery_receipt_refs
        checkpoint_tool_results = tool_call_result.checkpoint_tool_results
        tool_cycle_fingerprint = _tool_cycle_fingerprint(checkpoint_tool_results)
        chain_model_tool_output_chars = tool_call_result.chain_model_tool_output_chars
        if round_state.hosted_followup:
            chain_model_tool_output_chars = _append_hosted_followup_tool_output(
                hosted_followup=round_state.hosted_followup,
                tool_outputs=tool_outputs,
                checkpoint_tool_results=checkpoint_tool_results,
                chain_model_tool_output_chars=chain_model_tool_output_chars,
            )
        self._dump_tool_outputs(round_state, input_payload, tool_outputs)
        if tool_call_result.terminal_result is not None:
            # A successful terminal tool is authoritative. Do not checkpoint
            # or create another Responses round after it.
            return RuntimeToolAdvance(
                input_payload=tool_outputs,
                previous_response_id=round_state.response.id,
                delivery_receipt_refs=[],
                checkpoint_index=checkpoint_index,
                chain_id=chain_id,
                chain_round=chain_round,
                chain_model_tool_output_chars=chain_model_tool_output_chars,
                tool_cycle_fingerprint=None,
                terminal_result=tool_call_result.terminal_result,
            )
        checkpoint_reason = (
            None
            if followup_input_messages or delivery_receipt_refs
            else _checkpoint_reason(
                self.checkpoint,
                chain_round=chain_round,
                usage=round_state.usage,
                chain_model_tool_output_chars=chain_model_tool_output_chars,
            )
        )
        if checkpoint_reason:
            next_checkpoint_index, next_chain_id, next_input_payload = await _checkpoint_restart(
                db=self.db,
                generation_run_id=self.generation_run_id,
                phase=self.phase,
                agent_name=self.agent_name,
                resume_context=self.checkpoint_resume_context,
                previous_chain_id=chain_id,
                next_checkpoint_index=checkpoint_index + 1,
                reason=checkpoint_reason,
                last_tool_results=checkpoint_tool_results,
                prompt_hash=self.prompt_hash,
            )
            return RuntimeToolAdvance(
                input_payload=next_input_payload,
                previous_response_id=None,
                delivery_receipt_refs=[],
                checkpoint_index=next_checkpoint_index,
                chain_id=next_chain_id,
                chain_round=0,
                chain_model_tool_output_chars=0,
                tool_cycle_fingerprint=tool_cycle_fingerprint,
            )
        return RuntimeToolAdvance(
            input_payload=[*tool_outputs, *followup_input_messages],
            previous_response_id=round_state.response.id,
            delivery_receipt_refs=delivery_receipt_refs,
            checkpoint_index=checkpoint_index,
            chain_id=chain_id,
            chain_round=chain_round,
            chain_model_tool_output_chars=chain_model_tool_output_chars,
            tool_cycle_fingerprint=tool_cycle_fingerprint,
        )

    def _dump_tool_outputs(
        self,
        round_state: RuntimeRoundState,
        input_payload: Any,
        tool_outputs: list[dict[str, Any]],
    ) -> None:
        dump_v2_debug(
            run_id=str(self.generation_run_id),
            phase=self.phase,
            agent_name=self.agent_name,
            dump_type=f"round-{round_state.round_idx + 1:02d}-tool-outputs",
            payload={
                **_runtime_debug_payload(
                    generation_run_id=self.generation_run_id,
                    phase=self.phase,
                    agent_name=self.agent_name,
                    round_idx=round_state.round_idx,
                    prompt_hash=self.prompt_hash,
                    system=self.system,
                    prompt=self.prompt,
                    schema_name=self.schema_model.__name__,
                    schema=self.schema,
                    tools=self.tools,
                    policy=self.policy,
                    input_payload=input_payload,
                    response=round_state.response,
                    usage=round_state.usage,
                    file_search_calls=round_state.file_search_calls,
                    web_search_calls=round_state.web_search_calls,
                    function_calls=round_state.function_calls,
                    hosted_followup=round_state.hosted_followup,
                ),
                "tool_outputs": tool_outputs,
            },
        )


def _tool_cycle_fingerprint(tool_results: list[dict[str, Any]]) -> str | None:
    if not tool_results:
        return None
    encoded = json.dumps(
        tool_results,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["RuntimeToolAdvance", "RuntimeToolRoundProcessor"]
