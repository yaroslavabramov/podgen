from __future__ import annotations

import json
from typing import Any

from app.generation_v2.runtime_checkpoint import compact_checkpoint_value
from app.generation_v2.runtime_tool_output import MODEL_FACING_CONTRACT_VERSION, tool_model_facing_schema

_MODEL_FACING_CONTRACT_VERSION = MODEL_FACING_CONTRACT_VERSION
_compact_checkpoint_value = compact_checkpoint_value
_tool_model_facing_schema = tool_model_facing_schema


def _runtime_compact_checkpoint_value() -> Any:
    return compact_checkpoint_value


def _runtime_tool_model_facing_schema() -> Any:
    return tool_model_facing_schema


def model_output_stats(
    tool_name: str,
    model_output: dict[str, Any],
    *,
    contract_violation: str | None = None,
) -> dict[str, Any]:
    encoded = json.dumps(model_output, ensure_ascii=False, default=str)
    return {
        "chars": len(encoded),
        "model_facing_schema": _runtime_tool_model_facing_schema()(tool_name),
        "contract_version": _MODEL_FACING_CONTRACT_VERSION,
        "contract_violation": contract_violation,
    }


def checkpoint_source_inventory(
    inventory: Any,
    *,
    source_ref_by_id: dict[str, str],
    limit: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for card in list(getattr(inventory, "source_cards", []) or [])[:limit]:
        source_id = str(getattr(card, "source_id", "") or "")
        items.append(
            {
                "source_ref": source_ref_by_id.get(source_id, "S??"),
                "title": checkpoint_trim(getattr(card, "title", ""), 140),
                "source_type": getattr(card, "source_type", None),
                "domain": getattr(card, "domain", None),
                "coverage_status": getattr(card, "coverage_status", None),
                "brief_hint": checkpoint_trim(getattr(card, "summary", ""), 160),
            }
        )
    return items


def checkpoint_trim(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def trace_output_with_runtime(
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


def compact_checkpoint_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    compact, _ = _runtime_compact_checkpoint_value()(
        arguments,
        max_string_chars=500,
        max_list_items=12,
        max_depth=3,
    )
    return compact if isinstance(compact, dict) else {"arguments": compact}
