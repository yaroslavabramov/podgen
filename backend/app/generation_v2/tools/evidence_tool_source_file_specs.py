from __future__ import annotations

from typing import Any

from app.generation_v2.runtime import AgentTool


def evidence_source_file_tools(owner: Any) -> list[AgentTool]:
    return [
        AgentTool(
            name="open_source_file",
            description=(
                "Attach the pinned native file for one source to this same continuous set-agent. Use it when native "
                "document structure, PDF pages, tables, footnotes, or layout matter. State one concrete reading_goal. "
                "The attachment is a reading surface only: it does not create evidence and does not prove that the "
                "whole file was read. A successful visible result means the file is attached in that same turn. "
                "After reading, save a reading note; capture exact evidence separately. If no "
                "native file is available, use read_source or search_source."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source_ref": {"type": "string", "minLength": 1},
                    "reading_goal": {"type": "string", "minLength": 1, "maxLength": 1200},
                },
                "required": ["source_ref", "reading_goal"],
            },
            handler=owner.open_source_file_tool,
        )
    ]


__all__ = ["evidence_source_file_tools"]
