from __future__ import annotations

from app.generation_v2.runtime import AgentTool
from app.generation_v2.tools.evidence_tool_read_specs import evidence_read_tools
from app.generation_v2.tools.evidence_tool_write_specs import evidence_write_tools


class EvidenceToolSpecsMixin:
    def agent_tools(self) -> list[AgentTool]:
        return [*evidence_read_tools(self), *evidence_write_tools(self)]


__all__ = ["EvidenceToolSpecsMixin"]
