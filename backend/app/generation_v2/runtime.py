from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import openai
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.debug_dump import dump_v2_debug
from app.models.generation_v2 import EvidenceItem, RunCorpusView, V2ToolTrace
from app.observability import safe_set_run, trace_span
from app.services.llm import _openai_estimated_cost_usd, _openai_usage, get_openai_client
from app.pipeline.step_control import assert_step_not_stopped

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class V2RuntimeUnavailable(RuntimeError):
    pass


class ToolOutputContractViolation(RuntimeError):
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
    max_chain_model_tool_output_chars: int = 20_000


_MODEL_TOOL_OUTPUT_MAX_CHARS = 12000
_MODEL_FACING_CONTRACT_VERSION = "v2.tool-output-contract.1"
_CHECKPOINT_STRING_MAX_CHARS = 2200
_CHECKPOINT_LIST_MAX_ITEMS = 24
_MODEL_TOOL_MAX_DEPTH = 5
_MODEL_HIDDEN_TOOL_KEYS = {
    "provider",
    "provider_file_id",
    "provider_vector_store_id",
    "provider_vector_store_file_id",
    "vector_store_provider_file_id",
    "provider_corpus_file_id",
    "model_read_file_id",
    "content_hash",
    "snippet_hash",
    "storage_path",
    "attributes",
    "raw_output",
    "schema",
}
_CHECKPOINT_TASK_BRIEF_CHARS = 4500
_CHECKPOINT_LAST_TOOL_RESULT_LIMIT = 12
_CHECKPOINT_STATE_ID_LIMIT = 40


class V2Runtime:
    """OpenAI-first structured runtime with hosted and local tools."""

    def __init__(self) -> None:
        self.model = settings.generation_v2_openai_model
        self.reasoning_effort = settings.generation_v2_openai_reasoning_effort
        self._prompt_hashes: dict[tuple[str, str], str] = {}
        self._fallback_agents: set[tuple[str, str]] = set()

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
        hosted_file_search_handler: Callable[[list[dict[str, Any]], str, str, str], Awaitable[dict[str, Any] | None]] | None = None,
        max_output_tokens: int = 12000,
        max_tool_rounds: int | None = None,
        temperature: float | None = None,
        checkpoint_policy: AgentCheckpointPolicy | None = None,
    ) -> T:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self._prompt_hashes[(phase, agent_name)] = prompt_hash
        if not settings.openai_api_key:
            raise V2RuntimeUnavailable("OPENAI_API_KEY is not configured")

        client = get_openai_client()
        policy = tool_policy or ToolPolicy(web_search=settings.generation_v2_enable_web_search)
        tools = self._tool_specs(vector_store_id=vector_store_id, local_tools=local_tools or [], policy=policy)
        schema = _strict_json_schema(schema_model.model_json_schema())
        input_payload: Any = prompt
        previous_response_id: str | None = None
        announced_hosted_evidence_ids: set[str] = set()
        checkpoint = checkpoint_policy or _default_checkpoint_policy(agent_name)
        checkpoint_index = 0
        chain_round = 0
        chain_model_tool_output_chars = 0
        chain_id = _chain_id(phase, agent_name, prompt_hash, checkpoint_index)

        tool_round_limit = max(1, max_tool_rounds or settings.generation_v2_max_tool_rounds)
        for round_idx in range(tool_round_limit):
            await assert_step_not_stopped()
            chain_round += 1
            started = time.perf_counter()
            with trace_span(
                f"generation_v2.{phase}.{agent_name}.responses_create",
                run_type="llm",
                inputs={
                    "prompt_chars": len(prompt),
                    "prompt_hash": prompt_hash,
                    "round": round_idx + 1,
                    "schema": schema_model.__name__,
                },
                metadata={
                    "provider": "openai",
                    "model": self.model,
                    "phase": phase,
                    "agent_name": agent_name,
                    "reasoning_effort": self.reasoning_effort,
                    "vector_store_id": vector_store_id or "",
                },
                tags=["generation-v2", "openai", "responses", f"phase:{phase}", f"agent:{agent_name}"],
            ) as run_tree:
                try:
                    response = await client.responses.create(
                        model=self.model,
                        instructions=system,
                        input=input_payload,
                        previous_response_id=previous_response_id or openai.NOT_GIVEN,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature if temperature is not None else openai.NOT_GIVEN,
                        reasoning={"effort": self.reasoning_effort} if self.reasoning_effort else openai.NOT_GIVEN,
                        tools=tools or openai.NOT_GIVEN,
                        parallel_tool_calls=False if local_tools else openai.NOT_GIVEN,
                        include=["file_search_call.results"] if vector_store_id else openai.NOT_GIVEN,
                        text={
                            "format": {
                                "type": "json_schema",
                                "name": schema_model.__name__,
                                "schema": schema,
                                "strict": True,
                            }
                        },
                    )
                except Exception as exc:
                    dump_v2_debug(
                        run_id=str(generation_run_id),
                        phase=phase,
                        agent_name=agent_name,
                        dump_type=f"round-{round_idx + 1:02d}-exception",
                        payload={
                            "run_id": str(generation_run_id),
                            "phase": phase,
                            "agent_name": agent_name,
                            "round": round_idx + 1,
                            "prompt_hash": prompt_hash,
                            "system": system,
                            "prompt": prompt,
                            "schema_name": schema_model.__name__,
                            "schema": schema,
                            "tools": [_tool_trace_name(tool) for tool in tools],
                            "tool_policy": _tool_policy_payload(policy),
                            "input_payload": input_payload,
                            "chain_id": chain_id,
                            "checkpoint_index": checkpoint_index,
                            "chain_round": chain_round,
                            "error": str(exc),
                        },
                    )
                    safe_set_run(run_tree, outputs={"status": "exception", "error": str(exc)})
                    raise
            latency_ms = (time.perf_counter() - started) * 1000
            usage = _openai_usage(response)
            estimated_cost_usd = _openai_estimated_cost_usd(self.model, usage)
            file_search_calls = _file_search_calls(response)
            web_search_calls = _web_search_calls(response)
            safe_set_run(
                run_tree,
                outputs={
                    "response_id": response.id,
                    "status": getattr(response, "status", None),
                    "output_text_chars": len(response.output_text or ""),
                    "file_search_call_count": len(file_search_calls),
                    "web_search_call_count": len(web_search_calls),
                    "estimated_cost_usd": estimated_cost_usd,
                    **usage,
                },
            )
            await save_tool_trace(
                db,
                generation_run_id=generation_run_id,
                phase=phase,
                agent_name=agent_name,
                tool_name="openai.responses.create",
                input_json={
                    "prompt_chars": len(prompt),
                    "prompt_hash": prompt_hash,
                    "round": round_idx + 1,
                    "tools": [_tool_trace_name(tool) for tool in tools],
                    "allowed_tool_names": [_tool_trace_name(tool) for tool in tools],
                    "tool_policy": _tool_policy_payload(policy),
                    "chain_id": chain_id,
                    "checkpoint_index": checkpoint_index,
                    "chain_round": chain_round,
                    "has_previous_response_id": bool(previous_response_id),
                },
                output_json={
                    "response_id": response.id,
                    "status": getattr(response, "status", None),
                    "output_text_chars": len(response.output_text or ""),
                    "file_search_call_count": len(file_search_calls),
                    "web_search_call_count": len(web_search_calls),
                    "file_search_calls": _compact_hosted_calls(file_search_calls),
                    "web_search_calls": _compact_hosted_calls(web_search_calls),
                    "chain_id": chain_id,
                    "checkpoint_index": checkpoint_index,
                    "chain_round": chain_round,
                    "estimated_cost_usd": estimated_cost_usd,
                    **usage,
                },
                provider_call_id=response.id,
                latency_ms=latency_ms,
                input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
                output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
                cost_usd=estimated_cost_usd,
            )
            hosted_summary: dict[str, Any] | None = None
            if file_search_calls and hosted_file_search_handler:
                hosted_summary = await hosted_file_search_handler(file_search_calls, response.id, phase, agent_name)
            hosted_followup = _hosted_evidence_followup_input(
                hosted_summary,
                announced_ids=announced_hosted_evidence_ids,
                tools=local_tools or [],
            )
            if hosted_followup:
                announced_hosted_evidence_ids.update(hosted_followup["evidence_ids"])

            function_calls = _function_calls(response)
            if not function_calls:
                if hosted_followup:
                    hosted_chars = len(json.dumps(hosted_followup["message"], ensure_ascii=False))
                    chain_model_tool_output_chars += hosted_chars
                    checkpoint_reason = _checkpoint_reason(
                        checkpoint,
                        chain_round=chain_round,
                        usage=usage,
                        chain_model_tool_output_chars=chain_model_tool_output_chars,
                    )
                    dump_v2_debug(
                        run_id=str(generation_run_id),
                        phase=phase,
                        agent_name=agent_name,
                        dump_type=f"round-{round_idx + 1:02d}-hosted-followup",
                        payload=_runtime_debug_payload(
                            generation_run_id=generation_run_id,
                            phase=phase,
                            agent_name=agent_name,
                            round_idx=round_idx,
                            prompt_hash=prompt_hash,
                            system=system,
                            prompt=prompt,
                            schema_name=schema_model.__name__,
                            schema=schema,
                            tools=tools,
                            policy=policy,
                            input_payload=input_payload,
                            response=response,
                            usage=usage,
                            file_search_calls=file_search_calls,
                            web_search_calls=web_search_calls,
                            function_calls=function_calls,
                            hosted_followup=hosted_followup,
                        ),
                    )
                    if checkpoint_reason:
                        checkpoint_index, chain_id, input_payload = await _checkpoint_restart(
                            db=db,
                            generation_run_id=generation_run_id,
                            phase=phase,
                            agent_name=agent_name,
                            prompt=prompt,
                            previous_chain_id=chain_id,
                            next_checkpoint_index=checkpoint_index + 1,
                            reason=checkpoint_reason,
                            last_tool_results=[
                                {
                                    "tool_name": "hosted_file_search",
                                    "result": hosted_followup["message"].get("content"),
                                }
                            ],
                            prompt_hash=prompt_hash,
                        )
                        previous_response_id = None
                        chain_round = 0
                        chain_model_tool_output_chars = 0
                    else:
                        previous_response_id = response.id
                        input_payload = [hosted_followup["message"]]
                    continue
                try:
                    parsed = schema_model.model_validate_json(response.output_text or "{}")
                    dump_v2_debug(
                        run_id=str(generation_run_id),
                        phase=phase,
                        agent_name=agent_name,
                        dump_type=f"round-{round_idx + 1:02d}-parsed",
                        payload={
                            **_runtime_debug_payload(
                                generation_run_id=generation_run_id,
                                phase=phase,
                                agent_name=agent_name,
                                round_idx=round_idx,
                                prompt_hash=prompt_hash,
                                system=system,
                                prompt=prompt,
                                schema_name=schema_model.__name__,
                                schema=schema,
                                tools=tools,
                                policy=policy,
                                input_payload=input_payload,
                                response=response,
                                usage=usage,
                                file_search_calls=file_search_calls,
                                web_search_calls=web_search_calls,
                                function_calls=function_calls,
                                hosted_followup=hosted_followup,
                            ),
                            "parsed": parsed.model_dump(mode="json"),
                        },
                    )
                    return parsed
                except ValidationError as exc:
                    logger.error("V2 structured response failed validation: %s", response.output_text)
                    dump_v2_debug(
                        run_id=str(generation_run_id),
                        phase=phase,
                        agent_name=agent_name,
                        dump_type=f"round-{round_idx + 1:02d}-validation-error",
                        payload={
                            **_runtime_debug_payload(
                                generation_run_id=generation_run_id,
                                phase=phase,
                                agent_name=agent_name,
                                round_idx=round_idx,
                                prompt_hash=prompt_hash,
                                system=system,
                                prompt=prompt,
                                schema_name=schema_model.__name__,
                                schema=schema,
                                tools=tools,
                                policy=policy,
                                input_payload=input_payload,
                                response=response,
                                usage=usage,
                                file_search_calls=file_search_calls,
                                web_search_calls=web_search_calls,
                                function_calls=function_calls,
                                hosted_followup=hosted_followup,
                            ),
                            "validation_error": str(exc),
                        },
                    )
                    raise

            local_tool_map = {tool.name: tool for tool in local_tools or []}
            tool_outputs = []
            checkpoint_tool_results: list[dict[str, Any]] = []
            for call in function_calls:
                tool = local_tool_map.get(call["name"])
                if tool is None:
                    output = {"error": f"Unknown local tool: {call['name']}"}
                else:
                    try:
                        output = await tool.handler(call["arguments"])
                    except Exception as exc:
                        logger.warning("V2 local tool failed: %s", call["name"], exc_info=True)
                        output = {"error": str(exc)}
                try:
                    model_output = _model_facing_tool_output(call["name"], output)
                    output_stats = _model_output_stats(call["name"], model_output)
                except ToolOutputContractViolation as exc:
                    output_stats = _model_output_stats(call["name"], output, contract_violation=str(exc))
                    await save_tool_trace(
                        db,
                        generation_run_id=generation_run_id,
                        phase=phase,
                        agent_name=agent_name,
                        tool_name=call["name"],
                        input_json=call["arguments"],
                        output_json=_trace_output_with_runtime(
                            output,
                            output_stats,
                            chain_id=chain_id,
                            checkpoint_index=checkpoint_index,
                            chain_round=chain_round,
                        ),
                        provider_call_id=call["call_id"],
                    )
                    raise
                chain_model_tool_output_chars += output_stats["chars"]
                await save_tool_trace(
                    db,
                    generation_run_id=generation_run_id,
                    phase=phase,
                    agent_name=agent_name,
                    tool_name=call["name"],
                    input_json=call["arguments"],
                    output_json=_trace_output_with_runtime(output, output_stats, chain_id=chain_id, checkpoint_index=checkpoint_index, chain_round=chain_round),
                    provider_call_id=call["call_id"],
                )
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(model_output, ensure_ascii=False),
                    }
                )
                checkpoint_tool_results.append(
                    {
                        "tool_name": call["name"],
                        "arguments": _compact_checkpoint_arguments(call["arguments"]),
                        "result": model_output,
                    }
                )
            if hosted_followup:
                hosted_chars = len(json.dumps(hosted_followup["message"], ensure_ascii=False))
                chain_model_tool_output_chars += hosted_chars
                tool_outputs.append(hosted_followup["message"])
                checkpoint_tool_results.append(
                    {
                        "tool_name": "hosted_file_search",
                        "result": hosted_followup["message"].get("content"),
                    }
                )
            dump_v2_debug(
                run_id=str(generation_run_id),
                phase=phase,
                agent_name=agent_name,
                dump_type=f"round-{round_idx + 1:02d}-tool-outputs",
                payload={
                    **_runtime_debug_payload(
                        generation_run_id=generation_run_id,
                        phase=phase,
                        agent_name=agent_name,
                        round_idx=round_idx,
                        prompt_hash=prompt_hash,
                        system=system,
                        prompt=prompt,
                        schema_name=schema_model.__name__,
                        schema=schema,
                        tools=tools,
                        policy=policy,
                        input_payload=input_payload,
                        response=response,
                        usage=usage,
                        file_search_calls=file_search_calls,
                        web_search_calls=web_search_calls,
                        function_calls=function_calls,
                        hosted_followup=hosted_followup,
                    ),
                    "tool_outputs": tool_outputs,
                },
            )
            checkpoint_reason = _checkpoint_reason(
                checkpoint,
                chain_round=chain_round,
                usage=usage,
                chain_model_tool_output_chars=chain_model_tool_output_chars,
            )
            if checkpoint_reason:
                checkpoint_index, chain_id, input_payload = await _checkpoint_restart(
                    db=db,
                    generation_run_id=generation_run_id,
                    phase=phase,
                    agent_name=agent_name,
                    prompt=prompt,
                    previous_chain_id=chain_id,
                    next_checkpoint_index=checkpoint_index + 1,
                    reason=checkpoint_reason,
                    last_tool_results=checkpoint_tool_results,
                    prompt_hash=prompt_hash,
                )
                previous_response_id = None
                chain_round = 0
                chain_model_tool_output_chars = 0
            else:
                previous_response_id = response.id
                input_payload = tool_outputs

        raise RuntimeError(f"{agent_name} exceeded V2 tool round budget")

    def _tool_specs(
        self,
        *,
        vector_store_id: str | None,
        local_tools: list[AgentTool],
        policy: ToolPolicy,
    ) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if vector_store_id and policy.file_search:
            tools.append(
                {
                    "type": "file_search",
                    "vector_store_ids": [vector_store_id],
                    "filters": {
                        "type": "and",
                        "filters": [
                            {"type": "eq", "key": "is_active", "value": True},
                            {"type": "eq", "key": "agent_facing", "value": True},
                            {"type": "eq", "key": "canonical_retrieval_surface", "value": True},
                        ],
                    },
                    "max_num_results": policy.max_file_search_results,
                }
            )
        if policy.web_search:
            tools.append({"type": "web_search"})
        for tool in local_tools if policy.local_tools else []:
            tools.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": _strict_json_schema(tool.parameters),
                    "strict": True,
                }
            )
        return tools


def _default_checkpoint_policy(agent_name: str) -> AgentCheckpointPolicy:
    if agent_name in {"source_triage_agent", "research_agent"}:
        return AgentCheckpointPolicy(enabled=True)
    return AgentCheckpointPolicy(enabled=False)


def _chain_id(phase: str, agent_name: str, prompt_hash: str, checkpoint_index: int) -> str:
    return f"{phase}:{agent_name}:{prompt_hash[:12]}:{checkpoint_index}"


def _checkpoint_reason(
    policy: AgentCheckpointPolicy,
    *,
    chain_round: int,
    usage: dict[str, Any],
    chain_model_tool_output_chars: int,
) -> str | None:
    if not policy.enabled:
        return None
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    if input_tokens >= policy.max_chain_input_tokens:
        return f"input_tokens>={policy.max_chain_input_tokens}"
    if chain_round >= policy.max_chain_rounds:
        return f"chain_rounds>={policy.max_chain_rounds}"
    if chain_model_tool_output_chars >= policy.max_chain_model_tool_output_chars:
        return f"model_tool_output_chars>={policy.max_chain_model_tool_output_chars}"
    return None


async def _checkpoint_restart(
    *,
    db: AsyncSession,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
    prompt: str,
    previous_chain_id: str,
    next_checkpoint_index: int,
    reason: str,
    last_tool_results: list[dict[str, Any]],
    prompt_hash: str,
) -> tuple[int, str, list[dict[str, Any]]]:
    state = await _build_agent_checkpoint_state(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
    )
    next_chain_id = _chain_id(phase, agent_name, prompt_hash, next_checkpoint_index)
    message = _checkpoint_restart_message(
        prompt=prompt,
        reason=reason,
        previous_chain_id=previous_chain_id,
        next_chain_id=next_chain_id,
        checkpoint_index=next_checkpoint_index,
        state=state,
        last_tool_results=last_tool_results,
    )
    await save_tool_trace(
        db,
        generation_run_id=generation_run_id,
        phase=phase,
        agent_name=agent_name,
        tool_name="runtime.checkpoint_restart",
        input_json={
            "reason": reason,
            "previous_chain_id": previous_chain_id,
            "next_chain_id": next_chain_id,
            "checkpoint_index": next_checkpoint_index,
            "last_tool_result_count": len(last_tool_results),
        },
        output_json={
            "checkpoint_index": next_checkpoint_index,
            "chain_id": next_chain_id,
            "state": state,
            "message_chars": len(message["content"]),
        },
    )
    return next_checkpoint_index, next_chain_id, [message]


def _checkpoint_restart_message(
    *,
    prompt: str,
    reason: str,
    previous_chain_id: str,
    next_chain_id: str,
    checkpoint_index: int,
    state: dict[str, Any],
    last_tool_results: list[dict[str, Any]],
) -> dict[str, str]:
    payload = {
        "checkpoint_reason": reason,
        "previous_chain_id": previous_chain_id,
        "next_chain_id": next_chain_id,
        "checkpoint_index": checkpoint_index,
        "task_brief": _checkpoint_task_brief(prompt),
        "compact_state_from_db": state,
        "last_tool_results": last_tool_results[:_CHECKPOINT_LAST_TOOL_RESULT_LIMIT],
        "instructions": (
            "Continue the same agent task from this compact checkpoint. "
            "Do not restart from scratch. Treat DB state and write actions as authoritative. "
            "Do not repeat list_sources pages already shown in source_inventory_progress; continue from next_cursor when needed. "
            "Proceed to selection and write actions when enough evidence/source coverage exists."
        ),
    }
    compact, _ = _compact_checkpoint_value(
        payload,
        max_string_chars=1800,
        max_list_items=30,
        max_depth=6,
    )
    return {
        "role": "user",
        "content": "Responses chain checkpoint restart. Continue from compact state:\n"
        + json.dumps(compact, ensure_ascii=False),
    }


def _checkpoint_task_brief(prompt: str) -> str:
    text = " ".join((prompt or "").split())
    if len(text) <= _CHECKPOINT_TASK_BRIEF_CHARS:
        return text
    return text[:_CHECKPOINT_TASK_BRIEF_CHARS].rstrip() + "..."


async def _build_agent_checkpoint_state(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    phase: str,
    agent_name: str,
) -> dict[str, Any]:
    state: dict[str, Any] = {"phase": phase, "agent_name": agent_name}
    try:
        evidence_result = await db.execute(select(EvidenceItem).where(EvidenceItem.generation_run_id == generation_run_id))
        evidence_items = list(evidence_result.scalars())
        selected = [
            item
            for item in evidence_items
            if (item.metadata_ or {}).get("usage_status") in {"selected", "cited"}
        ]
        state["evidence"] = {
            "observed_count": len(evidence_items),
            "selected_count": len(selected),
            "selected_evidence_ids": [str(item.id) for item in selected[:_CHECKPOINT_STATE_ID_LIMIT]],
            "covered_source_ids": sorted({str(item.source_id) for item in selected})[:_CHECKPOINT_STATE_ID_LIMIT],
        }
    except Exception as exc:
        state["evidence_error"] = str(exc)

    run_view: RunCorpusView | None = None
    try:
        run_view_result = await db.execute(
            select(RunCorpusView).where(RunCorpusView.generation_run_id == generation_run_id)
        )
        run_view = run_view_result.scalar_one_or_none()
        if run_view:
            state["corpus"] = {
                "selected_source_count": len(run_view.selected_source_ids or []),
                "selected_source_version_count": len(run_view.selected_source_version_ids or []),
                "manifest_hash": run_view.manifest_hash,
                "selected_source_ids": [str(item) for item in (run_view.selected_source_ids or [])[:_CHECKPOINT_STATE_ID_LIMIT]],
            }
    except Exception as exc:
        state["corpus_error"] = str(exc)

    try:
        counts_result = await db.execute(
            select(V2ToolTrace.tool_name, func.count(V2ToolTrace.id))
            .where(
                V2ToolTrace.generation_run_id == generation_run_id,
                V2ToolTrace.phase == phase,
                V2ToolTrace.agent_name == agent_name,
            )
            .group_by(V2ToolTrace.tool_name)
        )
        state["tool_call_counts"] = {str(name): int(count) for name, count in counts_result.all()}
    except Exception as exc:
        state["tool_count_error"] = str(exc)

    try:
        list_result = await db.execute(
            select(V2ToolTrace)
            .where(
                V2ToolTrace.generation_run_id == generation_run_id,
                V2ToolTrace.phase == phase,
                V2ToolTrace.agent_name == agent_name,
                V2ToolTrace.tool_name == "list_sources",
            )
            .order_by(V2ToolTrace.created_at.asc())
        )
        seen_cursors: list[str] = []
        seen_source_ids: list[str] = []
        total_source_count = None
        next_cursor = ""
        for trace in list_result.scalars():
            input_json = trace.input_json or {}
            output_json = trace.output_json or {}
            cursor = str(input_json.get("cursor") or "")
            if cursor not in seen_cursors:
                seen_cursors.append(cursor)
            if output_json.get("total") is not None:
                total_source_count = int(output_json.get("total") or 0)
            next_cursor = str(output_json.get("next_cursor") or "")
            for source in output_json.get("sources") or []:
                source_id = str(source.get("source_id") or "")
                if source_id and source_id not in seen_source_ids:
                    seen_source_ids.append(source_id)
        if seen_cursors or seen_source_ids:
            state["source_inventory_progress"] = {
                "listed_source_count": len(seen_source_ids),
                "total_source_count": total_source_count,
                "seen_cursors": seen_cursors[:_CHECKPOINT_STATE_ID_LIMIT],
                "next_cursor": next_cursor,
                "all_sources_listed": bool(total_source_count is not None and len(seen_source_ids) >= total_source_count),
                "listed_source_ids": seen_source_ids[:_CHECKPOINT_STATE_ID_LIMIT],
            }
    except Exception as exc:
        state["source_inventory_progress_error"] = str(exc)

    if agent_name == "source_triage_agent" and run_view is not None:
        try:
            from app.generation_v2.tools.evidence_source_map import build_source_inventory, build_source_map_from_write_actions

            inventory = await build_source_inventory(db, run_view)
            source_map = await build_source_map_from_write_actions(
                db,
                generation_run_id=generation_run_id,
                inventory=inventory,
            )
            mapped_source_ids = [card.source_id for card in source_map.source_cards]
            selected_source_ids = [str(item) for item in run_view.selected_source_ids or []]
            state["source_map"] = {
                "source_card_count": len(source_map.source_cards),
                "quote_card_count": len(source_map.quote_cards),
                "relationship_count": len(source_map.source_triage.source_relationships),
                "gap_count": len(source_map.gaps),
                "mapped_source_ids": mapped_source_ids[:_CHECKPOINT_STATE_ID_LIMIT],
                "remaining_source_ids": [
                    source_id for source_id in selected_source_ids if source_id not in set(mapped_source_ids)
                ][: _CHECKPOINT_STATE_ID_LIMIT],
                "retrieval_notes": source_map.retrieval_notes[:6],
            }
        except Exception as exc:
            state["source_map_error"] = str(exc)
    return state


def _model_output_stats(
    tool_name: str,
    model_output: dict[str, Any],
    *,
    contract_violation: str | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(model_output, ensure_ascii=False, default=str)
    return {
        "chars": len(encoded),
        "model_facing_schema": _tool_model_facing_schema(tool_name),
        "contract_version": _MODEL_FACING_CONTRACT_VERSION,
        "contract_violation": contract_violation,
    }


def _trace_output_with_runtime(
    output: dict[str, Any],
    stats: dict[str, Any],
    *,
    chain_id: str,
    checkpoint_index: int,
    chain_round: int,
) -> dict[str, Any]:
    trace_output = dict(output)
    trace_output["_runtime"] = {
        "chain_id": chain_id,
        "checkpoint_index": checkpoint_index,
        "chain_round": chain_round,
        "model_facing_tool_output_chars": stats["chars"],
        "model_facing_schema": stats["model_facing_schema"],
        "contract_version": stats["contract_version"],
    }
    if stats.get("contract_violation"):
        trace_output["_runtime"]["contract_violation"] = stats["contract_violation"]
    return trace_output


def _compact_checkpoint_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    compact, _ = _compact_checkpoint_value(
        arguments,
        max_string_chars=500,
        max_list_items=12,
        max_depth=3,
    )
    return compact if isinstance(compact, dict) else {"arguments": compact}


def _function_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if _item_get(item, "type") != "function_call":
            continue
        raw_arguments = _item_get(item, "arguments", "{}") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {"_raw": raw_arguments}
        calls.append(
            {
                "call_id": _item_get(item, "call_id", "") or _item_get(item, "id", ""),
                "name": _item_get(item, "name", ""),
                "arguments": arguments,
            }
        )
    return calls


def _file_search_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if _item_get(item, "type") != "file_search_call":
            continue
        raw_results = _item_get(item, "results", []) or []
        results = []
        for idx, result in enumerate(raw_results):
            result_dict = _to_plain_dict(result)
            result_dict.setdefault("result_index", idx)
            results.append(result_dict)
        calls.append(
            {
                "call_id": _item_get(item, "id", "") or _item_get(item, "call_id", ""),
                "queries": _item_get(item, "queries", []) or [],
                "status": _item_get(item, "status", None),
                "results": results,
            }
        )
    return calls


def _web_search_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        if _item_get(item, "type") != "web_search_call":
            continue
        payload = _to_plain_dict(item)
        calls.append(
            {
                "call_id": _item_get(item, "id", "") or _item_get(item, "call_id", ""),
                "status": _item_get(item, "status", None),
                "action": payload.get("action"),
                "queries": payload.get("queries") or payload.get("query") or [],
            }
        )
    return calls


def _compact_hosted_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = []
    for call in calls[:12]:
        compact.append(
            {
                "call_id": call.get("call_id"),
                "status": call.get("status"),
                "queries": call.get("queries"),
                "result_count": len(call.get("results") or []),
            }
        )
    return compact


def _hosted_evidence_followup_input(
    summary: dict[str, Any] | None,
    *,
    announced_ids: set[str],
    tools: list[AgentTool],
) -> dict[str, Any] | None:
    if not summary or not _has_tool(tools, "select_evidence"):
        return None
    observed = summary.get("observed_evidence") or []
    new_items = [
        {
            "evidence_id": str(item.get("id") or item.get("evidence_id") or ""),
            "source_id": item.get("source_id"),
            "source_version_id": item.get("source_version_id"),
            "representation_kind": item.get("representation_kind"),
            "locator": item.get("locator") or item.get("locator_text"),
            "retrieval_channel": item.get("retrieval_channel"),
            "evidence_strength": item.get("evidence_strength"),
            "score": item.get("score"),
            "short_snippet": item.get("short_snippet") or str(item.get("snippet_text") or "")[:700],
        }
        for item in observed
        if str(item.get("id") or item.get("evidence_id") or "") and str(item.get("id") or item.get("evidence_id") or "") not in announced_ids
    ]
    if not new_items:
        return None
    evidence_ids = [item["evidence_id"] for item in new_items]
    content = (
        "Hosted file_search results were persisted as observed EvidenceItem records. "
        "Use these exact evidence_id values with select_evidence and source-map write tools before finalizing. "
        "For important PDFs/documents or citation-sensitive claims, call read_source_with_model with the source_id/source_version_id and inspect retrieval_channel before selecting/linking. "
        "If it returns model_file_read_local_fallback, treat that channel as a risk/coverage signal and reflect it in confidence/support rather than pretending it was a direct file read. "
        "Do not invent evidence ids.\n"
        + json.dumps({"observed_evidence": new_items}, ensure_ascii=False)
    )
    return {
        "evidence_ids": evidence_ids,
        "message": {"role": "user", "content": content},
    }


def _has_tool(tools: list[AgentTool], name: str) -> bool:
    return any(tool.name == name for tool in tools)


def _model_facing_tool_output(tool_name: str, output: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ToolOutputContractViolation(f"{tool_name} returned non-object output")
    if _contains_key(output, "_model_output_truncated"):
        raise ToolOutputContractViolation(f"{tool_name} returned legacy truncation marker")
    forbidden_keys = _forbidden_tool_output_keys(output)
    if forbidden_keys:
        keys = ", ".join(sorted(forbidden_keys)[:12])
        raise ToolOutputContractViolation(f"{tool_name} returned forbidden model-facing keys: {keys}")
    encoded = json.dumps(output, ensure_ascii=False, default=str)
    if len(encoded) > _MODEL_TOOL_OUTPUT_MAX_CHARS:
        raise ToolOutputContractViolation(
            f"{tool_name} model-facing output exceeded {_MODEL_TOOL_OUTPUT_MAX_CHARS} chars: {len(encoded)}"
        )
    return output


def _tool_model_facing_schema(tool_name: str) -> str:
    return {
        "list_sources": "source_inventory_page",
        "list_source_versions": "source_versions",
        "search_evidence": "evidence_preview_results",
        "search_sources": "source_search_summary",
        "find_quotes": "evidence_preview_results",
        "read_evidence": "evidence_read",
        "read_source": "evidence_read",
        "read_source_window": "evidence_read",
        "read_source_with_model": "model_read_evidence_read",
        "skim_source": "source_skim_windows",
        "inspect_source": "source_outline",
        "search_within_source": "evidence_preview_results",
        "compare_sources": "source_comparison_evidence",
        "select_evidence": "write_ack",
        "upsert_source_card": "write_ack",
        "create_quote_card": "write_ack",
        "record_source_relationship": "write_ack",
        "record_gap": "write_ack",
        "finalize_source_map": "write_ack",
    }.get(tool_name, "unknown_tool_output")


def _contains_key(value: Any, target_key: str) -> bool:
    if isinstance(value, dict):
        return any(str(key) == target_key or _contains_key(item, target_key) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_key(item, target_key) for item in value)
    return False


def _forbidden_tool_output_keys(value: Any) -> set[str]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key)
                if key_text in _MODEL_HIDDEN_TOOL_KEYS:
                    found.add(key_text)
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return found


def _compact_checkpoint_value(
    value: Any,
    *,
    max_string_chars: int,
    max_list_items: int,
    max_depth: int,
    depth: int = 0,
) -> tuple[Any, bool]:
    if depth > max_depth:
        return "[nested checkpoint state omitted]", True
    if isinstance(value, str):
        if len(value) <= max_string_chars:
            return value, False
        return value[:max_string_chars].rstrip() + "...", True
    if value is None or isinstance(value, (bool, int, float)):
        return value, False
    if isinstance(value, list):
        omitted = len(value) > max_list_items
        items = []
        for item in value[:max_list_items]:
            compact_item, item_omitted = _compact_checkpoint_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                depth=depth + 1,
            )
            items.append(compact_item)
            omitted = omitted or item_omitted
        return items, omitted
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        omitted = False
        for key, item in value.items():
            key_text = str(key)
            if key_text in _MODEL_HIDDEN_TOOL_KEYS:
                omitted = True
                continue
            compact_item, item_omitted = _compact_checkpoint_value(
                item,
                max_string_chars=max_string_chars,
                max_list_items=max_list_items,
                max_depth=max_depth,
                depth=depth + 1,
            )
            compact[key_text] = compact_item
            omitted = omitted or item_omitted
        return compact, omitted
    if hasattr(value, "model_dump"):
        return _compact_checkpoint_value(
            value.model_dump(mode="json"),
            max_string_chars=max_string_chars,
            max_list_items=max_list_items,
            max_depth=max_depth,
            depth=depth,
        )
    return str(value), False


def _collect_tool_ids(value: Any) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {
        "evidence_ids": [],
        "source_ids": [],
        "source_version_ids": [],
    }

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                text = str(item)
                if key in {"evidence_id", "id"} and "evidence_ids" in collected and len(text) < 80:
                    _append_unique(collected["evidence_ids"], text)
                elif key == "source_id" and len(text) < 80:
                    _append_unique(collected["source_ids"], text)
                elif key == "source_version_id" and len(text) < 80:
                    _append_unique(collected["source_version_ids"], text)
                elif key in {"evidence_ids", "source_ids", "source_version_ids"} and isinstance(item, list):
                    target = collected.get(key)
                    if target is not None:
                        for value_item in item[:20]:
                            if len(str(value_item)) < 80:
                                _append_unique(target, str(value_item))
                visit(item)
        elif isinstance(node, list):
            for item in node[:100]:
                visit(item)

    visit(value)
    return {key: values[:20] for key, values in collected.items() if values}


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _to_plain_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    data: dict[str, Any] = {}
    for key in (
        "file_id",
        "filename",
        "score",
        "text",
        "content",
        "attributes",
        "source_id",
        "source_version_id",
    ):
        value = getattr(item, key, None)
        if value is not None:
            data[key] = value
    return data


def _tool_trace_name(tool: dict[str, Any]) -> str:
    if tool.get("type") == "function":
        return str(tool.get("name") or "function")
    return str(tool.get("type") or tool.get("name") or "unknown")


def _tool_policy_payload(policy: ToolPolicy) -> dict[str, Any]:
    return {
        "file_search": policy.file_search,
        "web_search": policy.web_search,
        "local_tools": policy.local_tools,
        "max_file_search_results": policy.max_file_search_results,
    }


def _runtime_debug_payload(
    *,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_idx: int,
    prompt_hash: str,
    system: str,
    prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    tools: list[dict[str, Any]],
    policy: ToolPolicy,
    input_payload: Any,
    response: Any,
    usage: dict[str, Any],
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    function_calls: list[dict[str, Any]],
    hosted_followup: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "run_id": str(generation_run_id),
        "phase": phase,
        "agent_name": agent_name,
        "round": round_idx + 1,
        "prompt_hash": prompt_hash,
        "system": system,
        "prompt": prompt,
        "schema_name": schema_name,
        "schema": schema,
        "tools": [_tool_trace_name(tool) for tool in tools],
        "tool_policy": _tool_policy_payload(policy),
        "input_payload": input_payload,
        "response": {
            "id": getattr(response, "id", None),
            "status": getattr(response, "status", None),
            "output_text": getattr(response, "output_text", "") or "",
        },
        "usage": usage,
        "file_search_calls": file_search_calls,
        "web_search_calls": web_search_calls,
        "function_calls": function_calls,
        "hosted_followup": hosted_followup,
    }


def _item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Pydantic schema for OpenAI strict structured outputs."""
    schema = json.loads(json.dumps(schema))
    schema.pop("title", None)
    _strict_object(schema)
    return schema


def _strict_object(node: Any) -> None:
    if not isinstance(node, dict):
        return
    node.pop("default", None)
    node.pop("examples", None)
    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties") or {}
        node["additionalProperties"] = False
        node["required"] = list(properties.keys())
        for child in properties.values():
            _strict_object(child)
    for key in ("items", "anyOf", "oneOf", "allOf", "$defs"):
        value = node.get(key)
        if isinstance(value, list):
            for item in value:
                _strict_object(item)
        elif isinstance(value, dict):
            for item in (value.values() if key == "$defs" else [value]):
                _strict_object(item)
