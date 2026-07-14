from __future__ import annotations

from typing import Any

from app.generation_v2.tools.evidence_source_map_write_actions import (
    create_quote_card as _create_quote_card_action,
    finalize_source_map as _finalize_source_map_action,
    record_gap as _record_gap_action,
    record_source_cluster as _record_source_cluster_action,
    record_source_relationship as _record_source_relationship_action,
    upsert_source_card as _upsert_source_card_action,
)
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_SELECTED
from app.generation_v2.tools.evidence_utils import _unsupported_model_read_selected_as_support
from app.generation_v2.tools.evidence_write_validation import validate_source_map_write_args


class EvidenceWriteActionMixin:
    async def select_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        validation_error = validate_source_map_write_args("select_evidence", args)
        if validation_error:
            return validation_error
        evidence_id = str(args.get("evidence_id") or "")
        item = await self._evidence_in_run(evidence_id)
        if item is None:
            return {"accepted": False, "error": "Unknown evidence_id for this run."}
        purpose = str(args.get("purpose") or "")
        role = str(args.get("role") or "")
        if _unsupported_model_read_selected_as_support(item, purpose=purpose, role=role):
            return {
                "accepted": False,
                "error": "Unsupported/not_found model-read evidence can only be selected as a gap, caution, uncertainty, contradiction, or negative finding.",
            }
        await self._mark_evidence_status(
            evidence_id,
            EVIDENCE_STATUS_SELECTED,
            {
                "selected_purpose": purpose,
                "selected_role": role,
                "selected_rationale": str(args.get("rationale") or ""),
            },
        )
        try:
            selected_count = len(await self._selected_evidence_ids())
        except Exception:
            selected_count = 1
        return {
            "accepted": True,
            "evidence_id": evidence_id,
            "status": "selected",
            "selected_count": selected_count,
        }

    async def write_action_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": True}

    async def upsert_source_card_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _upsert_source_card_action(self, args)

    async def create_quote_card_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _create_quote_card_action(self, args)

    async def record_source_relationship_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _record_source_relationship_action(self, args)

    async def record_source_cluster_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _record_source_cluster_action(self, args)

    async def record_gap_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _record_gap_action(self, args)

    async def finalize_source_map_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _finalize_source_map_action(self, args)

__all__ = [
    "_create_quote_card_action",
    "_finalize_source_map_action",
    "_record_gap_action",
    "_record_source_cluster_action",
    "_record_source_relationship_action",
    "_upsert_source_card_action",
]
