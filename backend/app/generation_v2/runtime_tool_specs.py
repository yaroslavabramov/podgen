from __future__ import annotations

from typing import Any

from app.generation_v2.runtime_contracts import AgentTool, ToolPolicy
from app.generation_v2.runtime_schema import strict_json_schema as _strict_json_schema


def runtime_tool_specs(
    *,
    vector_store_id: str | None,
    local_tools: list[AgentTool],
    policy: ToolPolicy,
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    pinned_version_ids = tuple(
        dict.fromkeys(str(value).strip() for value in policy.file_search_source_version_ids if str(value).strip())
    )
    if vector_store_id and policy.file_search and pinned_version_ids:
        pinned_filter = {
            "type": "in",
            "key": "source_version_id",
            "value": list(pinned_version_ids),
        }
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
                        {"type": "eq", "key": "content_confirmed", "value": True},
                        pinned_filter,
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
