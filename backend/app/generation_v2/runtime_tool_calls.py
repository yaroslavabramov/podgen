from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.debug_dump import dump_v2_debug
from app.generation_v2.runtime_checkpoint_state import (
    commit_runtime_side_effects as _commit_runtime_side_effects,
    compact_checkpoint_arguments as _compact_checkpoint_arguments,
    model_output_stats as _model_output_stats,
    trace_output_with_runtime as _trace_output_with_runtime,
)
from app.generation_v2.runtime_contracts import (
    AgentTerminalResult,
    AgentTool,
    terminal_result_from_tool_output,
)
from app.generation_v2.runtime_debug import safe_tool_dump_name as _safe_tool_dump_name
from app.generation_v2.runtime_tool_output import (
    ToolOutputContractViolation,
    model_facing_tool_output as _model_facing_tool_output,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeToolCallResult:
    tool_outputs: list[dict[str, Any]]
    followup_input_messages: list[dict[str, Any]]
    delivery_receipt_refs: list[str]
    checkpoint_tool_results: list[dict[str, Any]]
    chain_model_tool_output_chars: int
    terminal_result: AgentTerminalResult | None = None


async def process_runtime_function_calls(
    db: AsyncSession,
    *,
    generation_run_id: Any,
    phase: str,
    agent_name: str,
    round_number: int,
    chain_id: str,
    checkpoint_index: int,
    chain_round: int,
    function_calls: list[dict[str, Any]],
    local_tools: list[AgentTool] | None,
    chain_model_tool_output_chars: int,
) -> RuntimeToolCallResult:
    local_tool_map = {tool.name: tool for tool in local_tools or []}
    tool_outputs: list[dict[str, Any]] = []
    followup_input_messages: list[dict[str, Any]] = []
    delivery_receipt_refs: list[str] = []
    checkpoint_tool_results: list[dict[str, Any]] = []
    accepted_terminal_result: AgentTerminalResult | None = None
    terminal_calls_in_batch = {
        call["call_id"]
        for call in function_calls
        if (tool := local_tool_map.get(call["name"])) is not None and tool.terminal
    }
    for call_index, call in enumerate(function_calls, start=1):
        debug_base = {
            "run_id": str(generation_run_id),
            "phase": phase,
            "agent_name": agent_name,
            "round": round_number,
            "chain_id": chain_id,
            "checkpoint_index": checkpoint_index,
            "chain_round": chain_round,
            "tool_index": call_index,
            "tool_name": call["name"],
            "call_id": call["call_id"],
            "arguments": call["arguments"],
            "function_call_status": call.get("status"),
            "arguments_valid": call.get("arguments_valid", True),
        }
        dump_v2_debug(
            run_id=str(generation_run_id),
            phase=phase,
            agent_name=agent_name,
            dump_type=f"round-{round_number:02d}-tool-{call_index:02d}-{_safe_tool_dump_name(call['name'])}-call",
            payload=debug_base,
        )
        tool = local_tool_map.get(call["name"])
        trace_only_error: dict[str, Any] | None = None
        call_status = call.get("status")
        arguments_valid = call.get("arguments_valid", True)
        if call["call_id"] in terminal_calls_in_batch and len(function_calls) > 1:
            output = {
                "accepted": False,
                "error": "terminal_tool_must_be_called_alone",
                "message": "Persist independent writes first, then call the terminal tool alone in a new response.",
            }
        elif call_status not in {None, "completed"}:
            output = {
                "accepted": False,
                "error": "incomplete_function_call",
                "message": "The provider did not complete this function call. Retry the call with complete arguments.",
            }
        elif not arguments_valid:
            output = {
                "accepted": False,
                "error": "invalid_function_arguments",
                "message": call.get("arguments_error") or "Function arguments were not valid JSON.",
            }
        elif tool is None:
            output = {"error": f"Unknown local tool: {call['name']}"}
        else:
            try:
                output = await tool.handler(call["arguments"])
            except Exception as exc:
                logger.warning("V2 local tool failed: %s", call["name"], exc_info=True)
                output = {
                    "error": "tool_execution_failed",
                    "message": "The tool failed internally. Retry once or choose another supported action.",
                }
                trace_only_error = {
                    "type": type(exc).__name__,
                    "detail": str(exc),
                }
        output_contract_error = None
        runtime_input_messages: list[dict[str, Any]] = []
        if isinstance(output, dict):
            contract_candidate = dict(output)
            has_runtime_messages = "_runtime_input_messages" in contract_candidate
            has_receipt_delivery = "_runtime_receipt_delivery" in contract_candidate
            raw_runtime_messages = contract_candidate.pop("_runtime_input_messages", [])
            raw_receipt_delivery = contract_candidate.pop("_runtime_receipt_delivery", None)
            trace_output = {
                **contract_candidate,
                **({"_runtime_error": trace_only_error} if trace_only_error else {}),
            }
            if (
                has_runtime_messages
                or has_receipt_delivery
                or call["name"] in _PRIVATE_RUNTIME_DELIVERY_TOOLS
            ):
                (
                    runtime_input_messages,
                    receipt_refs,
                    output_contract_error,
                ) = _validated_private_runtime_directive(
                    tool_name=call["name"],
                    output=contract_candidate,
                    raw_runtime_messages=raw_runtime_messages,
                    raw_receipt_delivery=raw_receipt_delivery,
                    has_runtime_messages=has_runtime_messages,
                    has_receipt_delivery=has_receipt_delivery,
                )
                trace_output["_runtime_delivery"] = {
                    "scheduled": output_contract_error is None,
                    **({"receipt_refs": receipt_refs} if receipt_refs else {}),
                }
                if receipt_refs and output_contract_error is None:
                    delivery_receipt_refs.extend(receipt_refs)
        else:
            output_contract_error = f"{call['name']} returned non-object output"
            trace_output = {
                "_runtime_error": {
                    "type": "ToolOutputContractViolation",
                    "detail": output_contract_error,
                },
                "result_type": type(output).__name__,
            }
        is_accepted_terminal = bool(
            isinstance(output, dict) and tool is not None and tool.terminal and output.get("accepted") is True
        )
        raw_output_chars = len(json.dumps(trace_output, ensure_ascii=False, default=str))
        dump_v2_debug(
            run_id=str(generation_run_id),
            phase=phase,
            agent_name=agent_name,
            dump_type=f"round-{round_number:02d}-tool-{call_index:02d}-{_safe_tool_dump_name(call['name'])}-raw-output",
            payload={
                **debug_base,
                "raw_output_chars": raw_output_chars,
                "raw_output": trace_output,
            },
        )
        try:
            if output_contract_error is not None:
                raise ToolOutputContractViolation(output_contract_error)
            # An accepted terminal result is consumed by the backend and is
            # never sent back to the model. Keep its completion payload out of
            # the model-facing output contract while retaining it in the raw
            # trace above.
            contract_output = (
                {"accepted": True, "terminal": True}
                if is_accepted_terminal
                else contract_candidate
            )
            model_output = _model_facing_tool_output(call["name"], contract_output)
            output_stats = _model_output_stats(call["name"], model_output)
        except ToolOutputContractViolation as exc:
            output_stats = _model_output_stats(call["name"], trace_output, contract_violation=str(exc))
            dump_v2_debug(
                run_id=str(generation_run_id),
                phase=phase,
                agent_name=agent_name,
                dump_type=f"round-{round_number:02d}-tool-{call_index:02d}-{_safe_tool_dump_name(call['name'])}-contract-violation",
                payload={
                    **debug_base,
                    "contract_violation": str(exc),
                    "model_output_stats": output_stats,
                    "raw_output_chars": raw_output_chars,
                    "raw_output": trace_output,
                },
            )
            await save_tool_trace(
                db,
                generation_run_id=generation_run_id,
                phase=phase,
                agent_name=agent_name,
                tool_name=call["name"],
                input_json=call["arguments"],
                output_json=_trace_output_with_runtime(
                    trace_output,
                    output_stats,
                    chain_id=chain_id,
                    checkpoint_index=checkpoint_index,
                    chain_round=chain_round,
                ),
                provider_call_id=call["call_id"],
            )
            await _commit_runtime_side_effects(db)
            raise
        chain_model_tool_output_chars += output_stats["chars"]
        await save_tool_trace(
            db,
            generation_run_id=generation_run_id,
            phase=phase,
            agent_name=agent_name,
            tool_name=call["name"],
            input_json=call["arguments"],
            output_json=_trace_output_with_runtime(
                trace_output,
                output_stats,
                chain_id=chain_id,
                checkpoint_index=checkpoint_index,
                chain_round=chain_round,
            ),
            provider_call_id=call["call_id"],
        )
        await _commit_runtime_side_effects(db)
        tool_outputs.append(
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": json.dumps(model_output, ensure_ascii=False),
            }
        )
        followup_input_messages.extend(runtime_input_messages)
        checkpoint_tool_results.append(
            {
                "tool_name": call["name"],
                "arguments": _compact_checkpoint_arguments(call["arguments"]),
                "result": model_output,
            }
        )
        if tool is not None and isinstance(output, dict):
            terminal_result = terminal_result_from_tool_output(
                tool,
                arguments=call["arguments"],
                output=output,
            )
            if terminal_result is not None and terminal_result.accepted:
                accepted_terminal_result = terminal_result
                break
    return RuntimeToolCallResult(
        tool_outputs=tool_outputs,
        followup_input_messages=followup_input_messages,
        delivery_receipt_refs=delivery_receipt_refs,
        checkpoint_tool_results=checkpoint_tool_results,
        chain_model_tool_output_chars=chain_model_tool_output_chars,
        terminal_result=accepted_terminal_result,
    )


_PRIVATE_RUNTIME_DELIVERY_TOOLS = frozenset(
    {"read_source", "search_source", "open_source_file"}
)


def _validated_private_runtime_directive(
    *,
    tool_name: str,
    output: dict[str, Any],
    raw_runtime_messages: Any,
    raw_receipt_delivery: Any,
    has_runtime_messages: bool,
    has_receipt_delivery: bool,
) -> tuple[list[dict[str, Any]], list[str], str | None]:
    if tool_name not in _PRIVATE_RUNTIME_DELIVERY_TOOLS:
        return [], [], f"{tool_name} is not allowed to return private runtime input"

    successful = (
        output.get("accepted") is True and output.get("attached") is True
        if tool_name == "open_source_file"
        else output.get("found") is True
    )
    if not successful:
        if has_runtime_messages or has_receipt_delivery:
            return [], [], f"{tool_name} may schedule private delivery only after success"
        return [], [], None

    if not has_receipt_delivery or not isinstance(raw_receipt_delivery, dict) or set(
        raw_receipt_delivery
    ) != {"receipt_refs"}:
        return [], [], f"{tool_name} returned invalid receipt delivery metadata"
    raw_receipt_refs = raw_receipt_delivery.get("receipt_refs")
    if (
        not isinstance(raw_receipt_refs, list)
        or not raw_receipt_refs
        or len(raw_receipt_refs) > 8
    ):
        return [], [], f"{tool_name} returned invalid delivery receipt_refs"
    receipt_refs = [str(value or "").strip() for value in raw_receipt_refs]
    if (
        len(set(receipt_refs)) != len(receipt_refs)
        or any(not value.startswith("receipt:") or len(value) > 128 for value in receipt_refs)
    ):
        return [], [], f"{tool_name} returned invalid delivery receipt_refs"

    expected_receipt_refs: list[str]
    if tool_name in {"read_source", "open_source_file"}:
        expected_receipt_refs = [str(output.get("receipt_ref") or "").strip()]
    else:
        results = output.get("results")
        if not isinstance(results, list):
            return [], [], "search_source returned invalid results for receipt delivery"
        expected_receipt_refs = [
            str(item.get("receipt_ref") or "").strip()
            for item in results
            if isinstance(item, dict)
        ]
        if len(expected_receipt_refs) != len(results):
            return [], [], "search_source returned invalid result receipt_refs"
    if receipt_refs != expected_receipt_refs:
        return [], [], f"{tool_name} delivery receipt_refs do not match its public output"

    if tool_name != "open_source_file":
        if has_runtime_messages:
            return [], [], f"{tool_name} is not allowed to inject private runtime messages"
        return [], receipt_refs, None

    if not has_runtime_messages or not isinstance(raw_runtime_messages, list) or len(
        raw_runtime_messages
    ) != 1:
        return [], [], "open_source_file must return exactly one private attachment message"
    message = raw_runtime_messages[0]
    if not isinstance(message, dict) or set(message) != {"role", "content"}:
        return [], [], "open_source_file returned invalid private attachment message"
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list) or len(content) != 2:
        return [], [], "open_source_file returned invalid private attachment content"
    text_part, file_part = content
    if (
        not isinstance(text_part, dict)
        or set(text_part) != {"type", "text"}
        or text_part.get("type") != "input_text"
        or not isinstance(text_part.get("text"), str)
        or not str(text_part.get("text") or "").strip()
    ):
        return [], [], "open_source_file returned invalid private attachment text"
    if (
        not isinstance(file_part, dict)
        or set(file_part) != {"type", "file_id"}
        or file_part.get("type") != "input_file"
        or not isinstance(file_part.get("file_id"), str)
        or not str(file_part.get("file_id") or "").strip()
        or len(str(file_part.get("file_id") or "")) > 255
    ):
        return [], [], "open_source_file returned invalid private attachment file"
    return [message], receipt_refs, None
