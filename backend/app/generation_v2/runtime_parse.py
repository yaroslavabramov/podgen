from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_debug import runtime_debug_payload as _runtime_debug_payload

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def parse_structured_runtime_response(
    *,
    schema_model: type[T],
    response: Any,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_idx: int,
    prompt_hash: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    tools: list[dict[str, Any]],
    policy: Any,
    input_payload: Any,
    usage: dict[str, Any],
    file_search_calls: list[dict[str, Any]],
    web_search_calls: list[dict[str, Any]],
    function_calls: list[dict[str, Any]],
    hosted_followup: dict[str, Any] | None,
) -> T:
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


__all__ = ["parse_structured_runtime_response"]
