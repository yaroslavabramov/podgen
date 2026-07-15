from __future__ import annotations

import hashlib
import itertools
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.runtime_contracts import (
    AgentCheckpointPolicy,
    AgentTerminalResult,
    AgentTool,
    CheckpointResumeContext,
    ToolPolicy,
    V2RuntimeUnavailable,
    default_checkpoint_policy as _default_checkpoint_policy,
    tool_round_limit as _tool_round_limit,
)
from app.generation_v2.runtime_checkpoint_context import validate_checkpoint_resume_context
from app.generation_v2.runtime_checkpoint_state import (
    chain_id as _chain_id,
)
from app.generation_v2.runtime_hosted_followup import (
    advance_hosted_followup_without_function_calls as _advance_hosted_followup_without_function_calls,
)
from app.generation_v2.runtime_parse import parse_structured_runtime_response as _parse_structured_runtime_response
from app.generation_v2.runtime_response_status import (
    output_continuation_message,
    response_error,
    response_incomplete_reason,
    response_status,
)
from app.generation_v2.runtime_round import RuntimeRoundRunner, RuntimeRoundState
from app.generation_v2.runtime_schema import strict_json_schema as _strict_json_schema
from app.generation_v2.runtime_tool_round import RuntimeToolRoundProcessor

T = TypeVar("T", bound=BaseModel)


class RuntimeCallSession(Generic[T]):
    def __init__(
        self,
        runtime: Any,
        *,
        db: AsyncSession,
        generation_run_id: Any,
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
        temperature: float | None = None,
        checkpoint_policy: AgentCheckpointPolicy | None = None,
        checkpoint_resume_context: CheckpointResumeContext | None = None,
    ) -> None:
        self.runtime = runtime
        self.db = db
        self.generation_run_id = generation_run_id
        self.phase = phase
        self.agent_name = agent_name
        self.schema_model = schema_model
        self.system = system
        self.prompt = prompt
        self.vector_store_id = vector_store_id
        self.local_tools = local_tools
        self.tool_policy = tool_policy
        self.hosted_file_search_handler = hosted_file_search_handler
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.checkpoint = checkpoint_policy or _default_checkpoint_policy(agent_name, phase=phase)
        self.checkpoint_resume_context = checkpoint_resume_context

        self.prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.input_payload: Any = prompt
        self.previous_response_id: str | None = None
        self.checkpoint_index = 0
        self.chain_round = 0
        self.chain_model_tool_output_chars = 0
        self.consecutive_output_exhaustions = 0
        self.chain_id = _chain_id(phase, agent_name, self.prompt_hash, self.checkpoint_index)

        self.client: Any | None = None
        self.requested_model = ""
        self.policy = ToolPolicy()
        self.tools: list[dict[str, Any]] = []
        self.schema: dict[str, Any] = {}
        self.tool_round_limit = _tool_round_limit(max_tool_rounds)
        # Output continuations do not consume semantic tool rounds, but they
        # still need an operational runaway guard. Reuse the configured global
        # provider-round budget instead of a hidden hard-coded continuation cap.
        self.output_continuation_limit = _tool_round_limit(None)
        self.round_runner: RuntimeRoundRunner | None = None
        self.tool_processor: RuntimeToolRoundProcessor | None = None

    async def run(self) -> T:
        self.runtime._prompt_hashes[(self.phase, self.agent_name)] = self.prompt_hash
        resumed = await self._resume_terminal_completion()
        if resumed is not None:
            return resumed
        if not settings.openai_api_key:
            raise V2RuntimeUnavailable("OPENAI_API_KEY is not configured")
        self._prepare_call()

        workflow_round_count = 0
        for round_idx in itertools.count():
            if self.tool_round_limit is not None and workflow_round_count >= self.tool_round_limit:
                break
            round_state = await self._run_round(round_idx)
            incomplete_without_calls = self._validate_response_status(round_state)
            if incomplete_without_calls:
                self._continue_after_output_exhaustion(round_state)
                continue
            self.consecutive_output_exhaustions = 0
            workflow_round_count += 1
            if not round_state.function_calls:
                parsed = await self._advance_hosted_followup_or_parse(round_state)
                if parsed is not None:
                    return parsed
                continue
            terminal_completion = await self._process_function_calls(round_state)
            if terminal_completion is not None:
                return terminal_completion

        raise RuntimeError(f"{self.agent_name} exceeded V2 tool round budget")

    def _validate_response_status(self, round_state: RuntimeRoundState) -> bool:
        status = response_status(round_state.response)
        if status == "completed":
            return False
        if status != "incomplete":
            provider_error = response_error(round_state.response)
            detail = provider_error.get("message") or provider_error.get("code")
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"{self.agent_name} received provider response status {status} "
                f"in round {round_state.round_idx + 1}{suffix}"
            )
        reason = response_incomplete_reason(round_state.response)
        if reason is None:
            output_tokens = int(round_state.usage.get("output_tokens") or 0)
            if self.max_output_tokens is not None and output_tokens >= self.max_output_tokens:
                reason = "max_output_tokens"
        if reason != "max_output_tokens":
            raise RuntimeError(
                f"{self.agent_name} received incomplete provider response "
                f"in round {round_state.round_idx + 1}: {reason or 'unknown_reason'}"
            )
        return not bool(round_state.function_calls)

    def _continue_after_output_exhaustion(self, round_state: RuntimeRoundState) -> None:
        if (
            self.output_continuation_limit is not None
            and self.consecutive_output_exhaustions >= self.output_continuation_limit
        ):
            attempts = self.consecutive_output_exhaustions + 1
            raise RuntimeError(
                f"{self.agent_name} exhausted "
                f"max_output_tokens={self.max_output_tokens or 'provider_native'} "
                f"for {attempts} consecutive provider responses"
            )
        self.consecutive_output_exhaustions += 1
        continuation = output_continuation_message()
        if round_state.hosted_followup:
            continuation.insert(0, round_state.hosted_followup["message"])
        self.input_payload = continuation
        self.previous_response_id = round_state.response.id

    def _prepare_call(self) -> None:
        if self.checkpoint_resume_context is not None:
            validate_checkpoint_resume_context(self.checkpoint_resume_context)
        self.requested_model = self.runtime.model_for(self.phase, self.agent_name)
        self.client = self.runtime._get_openai_client()
        self.policy = self.tool_policy or ToolPolicy(web_search=settings.generation_v2_enable_web_search)
        self.tools = self.runtime._tool_specs(
            vector_store_id=self.vector_store_id,
            local_tools=self.local_tools or [],
            policy=self.policy,
        )
        self.schema = _strict_json_schema(self.schema_model.model_json_schema())
        self.round_runner = RuntimeRoundRunner(
            runtime=self.runtime,
            client=self.client,
            db=self.db,
            generation_run_id=self.generation_run_id,
            phase=self.phase,
            agent_name=self.agent_name,
            prompt_hash=self.prompt_hash,
            system=self.system,
            prompt=self.prompt,
            schema_model=self.schema_model,
            schema=self.schema,
            tools=self.tools,
            policy=self.policy,
            requested_model=self.requested_model,
            vector_store_id=self.vector_store_id,
            local_tools=self.local_tools,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            hosted_file_search_handler=self.hosted_file_search_handler,
        )
        self.tool_processor = RuntimeToolRoundProcessor(
            runtime=self.runtime,
            db=self.db,
            generation_run_id=self.generation_run_id,
            phase=self.phase,
            agent_name=self.agent_name,
            prompt_hash=self.prompt_hash,
            system=self.system,
            prompt=self.prompt,
            schema_model=self.schema_model,
            schema=self.schema,
            tools=self.tools,
            policy=self.policy,
            local_tools=self.local_tools,
            checkpoint=self.checkpoint,
            checkpoint_resume_context=self.checkpoint_resume_context,
        )

    async def _run_round(self, round_idx: int) -> RuntimeRoundState:
        if self.round_runner is None:
            raise RuntimeError("RuntimeCallSession is not prepared")
        self.chain_round += 1
        return await self.round_runner.run_round(
            round_idx=round_idx,
            input_payload=self.input_payload,
            chain_id=self.chain_id,
            checkpoint_index=self.checkpoint_index,
            chain_round=self.chain_round,
            previous_response_id=self.previous_response_id,
        )

    async def _advance_hosted_followup_or_parse(self, round_state: RuntimeRoundState) -> T | None:
        if round_state.hosted_followup:
            hosted_advance = await _advance_hosted_followup_without_function_calls(
                db=self.db,
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
                input_payload=self.input_payload,
                response=round_state.response,
                usage=round_state.usage,
                file_search_calls=round_state.file_search_calls,
                web_search_calls=round_state.web_search_calls,
                function_calls=round_state.function_calls,
                hosted_followup=round_state.hosted_followup,
                checkpoint=self.checkpoint,
                checkpoint_resume_context=self.checkpoint_resume_context,
                checkpoint_index=self.checkpoint_index,
                chain_id=self.chain_id,
                chain_round=self.chain_round,
                chain_model_tool_output_chars=self.chain_model_tool_output_chars,
            )
            self.checkpoint_index = hosted_advance.checkpoint_index
            self.chain_id = hosted_advance.chain_id
            self.input_payload = hosted_advance.input_payload
            self.previous_response_id = hosted_advance.previous_response_id
            self.chain_round = hosted_advance.chain_round
            self.chain_model_tool_output_chars = hosted_advance.chain_model_tool_output_chars
            return None

        if any(tool.terminal for tool in self.local_tools or []):
            raise RuntimeError(
                f"{self.agent_name} returned structured output without completing its required terminal tool"
            )
        return _parse_structured_runtime_response(
            schema_model=self.schema_model,
            response=round_state.response,
            generation_run_id=self.generation_run_id,
            phase=self.phase,
            agent_name=self.agent_name,
            round_idx=round_state.round_idx,
            prompt_hash=self.prompt_hash,
            system=self.system,
            prompt=self.prompt,
            schema=self.schema,
            tools=self.tools,
            policy=self.policy,
            input_payload=self.input_payload,
            usage=round_state.usage,
            file_search_calls=round_state.file_search_calls,
            web_search_calls=round_state.web_search_calls,
            function_calls=round_state.function_calls,
            hosted_followup=round_state.hosted_followup,
        )

    async def _process_function_calls(self, round_state: RuntimeRoundState) -> T | None:
        if self.tool_processor is None:
            raise RuntimeError("RuntimeCallSession is not prepared")
        advance = await self.tool_processor.process(
            round_state,
            input_payload=self.input_payload,
            chain_id=self.chain_id,
            checkpoint_index=self.checkpoint_index,
            chain_round=self.chain_round,
            chain_model_tool_output_chars=self.chain_model_tool_output_chars,
        )
        if advance.terminal_result is not None:
            return self._validated_terminal_completion(
                advance.terminal_result,
                source="accepted terminal tool",
            )
        self.input_payload = advance.input_payload
        self.previous_response_id = advance.previous_response_id
        self.checkpoint_index = advance.checkpoint_index
        self.chain_id = advance.chain_id
        self.chain_round = advance.chain_round
        self.chain_model_tool_output_chars = advance.chain_model_tool_output_chars
        return None

    async def _resume_terminal_completion(self) -> T | None:
        resumed: list[tuple[str, AgentTerminalResult]] = []
        for tool in self.local_tools or []:
            if not tool.terminal or tool.terminal_resume_loader is None:
                continue
            terminal_result = await tool.terminal_resume_loader()
            if terminal_result is None or not terminal_result.accepted:
                continue
            resumed.append((tool.name, terminal_result))
        if not resumed:
            return None
        if len(resumed) > 1:
            names = ", ".join(name for name, _ in resumed)
            raise RuntimeError(f"Multiple terminal resume loaders completed for one agent call: {names}")
        tool_name, terminal_result = resumed[0]
        return self._validated_terminal_completion(
            terminal_result,
            source=f"terminal resume loader {tool_name!r}",
        )

    def _validated_terminal_completion(
        self,
        terminal_result: AgentTerminalResult,
        *,
        source: str,
    ) -> T:
        if not terminal_result.accepted or terminal_result.completion is None:
            raise RuntimeError(f"{source} did not provide an accepted terminal completion")
        try:
            if isinstance(terminal_result.completion, self.schema_model):
                return terminal_result.completion
            return self.schema_model.model_validate(terminal_result.completion)
        except Exception as exc:
            raise RuntimeError(
                f"{source} returned a completion that does not match {self.schema_model.__name__}"
            ) from exc


async def call_structured_runtime(
    runtime: Any,
    *,
    db: AsyncSession,
    generation_run_id: Any,
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
    temperature: float | None = None,
    checkpoint_policy: AgentCheckpointPolicy | None = None,
    checkpoint_resume_context: CheckpointResumeContext | None = None,
) -> T:
    return await RuntimeCallSession(
        runtime,
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
        temperature=temperature,
        checkpoint_policy=checkpoint_policy,
        checkpoint_resume_context=checkpoint_resume_context,
    ).run()


__all__ = ["RuntimeCallSession", "RuntimeRoundState", "call_structured_runtime"]
