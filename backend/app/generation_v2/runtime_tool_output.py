from __future__ import annotations

import json
from typing import Any


MODEL_TOOL_OUTPUT_MAX_CHARS = 12000
MODEL_FACING_CONTRACT_VERSION = "v2.tool-output-contract.1"
MODEL_HIDDEN_TOOL_KEYS = {
    "provider",
    "source_id",
    "source_version_id",
    "representation_kind",
    "retrieval_channel",
    "evidence_strength",
    "usage_status",
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


class ToolOutputContractViolation(RuntimeError):
    pass


def model_facing_tool_output(tool_name: str, output: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ToolOutputContractViolation(f"{tool_name} returned non-object output")
    if contains_key(output, "_model_output_truncated"):
        raise ToolOutputContractViolation(f"{tool_name} returned legacy truncation marker")
    forbidden_keys = forbidden_tool_output_keys(output)
    if forbidden_keys:
        keys = ", ".join(sorted(forbidden_keys)[:12])
        raise ToolOutputContractViolation(f"{tool_name} returned forbidden model-facing keys: {keys}")
    encoded = json.dumps(output, ensure_ascii=False, default=str)
    if len(encoded) > MODEL_TOOL_OUTPUT_MAX_CHARS:
        raise ToolOutputContractViolation(
            f"{tool_name} model-facing output exceeded {MODEL_TOOL_OUTPUT_MAX_CHARS} chars: {len(encoded)}"
        )
    return output


def tool_model_facing_schema(tool_name: str) -> str:
    return {
        "list_sources": "source_inventory_page",
        "search_corpus": "evidence_preview_results",
        "search_sources": "source_search_summary",
        "find_quotes": "evidence_preview_results",
        "read_evidence": "evidence_read",
        "list_audit_evidence": "audit_evidence_page",
        "list_research_workspace_items": "research_workspace_page",
        "read_source_window": "evidence_read",
        "read_source_with_model": "model_read_evidence_read",
        "skim_source": "source_skim_windows",
        "inspect_source": "source_outline",
        "search_within_source": "evidence_preview_results",
        "compare_source_evidence": "source_comparison_evidence",
        "compare_evidence_for_question": "research_comparison_evidence",
        "select_evidence": "write_ack",
        "upsert_research_job": "write_ack",
        "finalize_research_plan": "write_ack",
        "record_research_observation": "write_ack",
        "upsert_editorial_atom": "write_ack",
        "finish_research_job": "write_ack",
        "record_research_risk": "write_ack",
        "record_research_gap": "write_ack",
        "finalize_research_memo": "write_ack",
        "upsert_source_card": "write_ack",
        "create_quote_card": "write_ack",
        "record_source_relationship": "write_ack",
        "record_source_cluster": "write_ack",
        "record_gap": "write_ack",
        "list_source_map_gaps": "source_map_gap_page",
        "resolve_gap": "write_ack",
        "resolve_source_map_repair": "write_ack",
        "finalize_source_map": "write_ack",
        "ingest_source_candidate": "source_acquisition_ack",
        "reject_source_candidate": "source_acquisition_ack",
        "finalize_source_acquisition_round": "source_acquisition_round_ack",
    }.get(tool_name, "unknown_tool_output")


def contains_key(value: Any, target_key: str) -> bool:
    if isinstance(value, dict):
        return any(str(key) == target_key or contains_key(item, target_key) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_key(item, target_key) for item in value)
    return False


def forbidden_tool_output_keys(value: Any) -> set[str]:
    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key)
                if key_text in MODEL_HIDDEN_TOOL_KEYS:
                    found.add(key_text)
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return found
