"""Explicit native grammar policy; unmarked archived requests keep their exact wire."""

from .contracts import ProviderFault

KEY = "podgen_messages_tool_policy"
VERSION = "locally-validated-navigation/1"
# These read/discovery tools are validated against their full frozen JSON Schema
# before execution. Acquisition and durable checkpoint tools remain strict.
NAVIGATION = frozenset(
    {
        "search_sources",
        "read_source",
        "read_material",
        "list_research_results",
        "list_research_sources",
        "select_research_result",
        "list_sources",
        "list_work_history",
        "read_work_history",
        "read_research_result",
    }
)


def apply(tools, metadata):
    policy = metadata.get(KEY)
    if policy is None:
        return tools
    if policy != VERSION:
        raise ProviderFault("unsupported_messages_tool_policy")
    return [dict(tool, strict=False) if tool["name"] in NAVIGATION else tool for tool in tools]
