from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import openai
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.services.llm import _openai_usage, get_openai_client
from app.pipeline.step_control import assert_step_not_stopped

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


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
        max_output_tokens: int = 12000,
        max_tool_rounds: int | None = None,
        temperature: float | None = None,
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

        tool_round_limit = max(1, max_tool_rounds or settings.generation_v2_max_tool_rounds)
        for round_idx in range(tool_round_limit):
            await assert_step_not_stopped()
            started = time.perf_counter()
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
            latency_ms = (time.perf_counter() - started) * 1000
            usage = _openai_usage(response)
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
                    "tool_policy": {
                        "file_search": policy.file_search,
                        "web_search": policy.web_search,
                        "local_tools": policy.local_tools,
                        "max_file_search_results": policy.max_file_search_results,
                    },
                },
                output_json={
                    "response_id": response.id,
                    "status": getattr(response, "status", None),
                    "output_text_chars": len(response.output_text or ""),
                    **usage,
                },
                provider_call_id=response.id,
                latency_ms=latency_ms,
                input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
                output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
            )

            function_calls = _function_calls(response)
            if not function_calls:
                try:
                    return schema_model.model_validate_json(response.output_text or "{}")
                except ValidationError:
                    logger.error("V2 structured response failed validation: %s", response.output_text)
                    raise

            local_tool_map = {tool.name: tool for tool in local_tools or []}
            tool_outputs = []
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
                await save_tool_trace(
                    db,
                    generation_run_id=generation_run_id,
                    phase=phase,
                    agent_name=agent_name,
                    tool_name=call["name"],
                    input_json=call["arguments"],
                    output_json=output,
                    provider_call_id=call["call_id"],
                )
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(output, ensure_ascii=False),
                    }
                )
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


def _tool_trace_name(tool: dict[str, Any]) -> str:
    if tool.get("type") == "function":
        return str(tool.get("name") or "function")
    return str(tool.get("type") or tool.get("name") or "unknown")


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
