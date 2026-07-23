from __future__ import annotations

from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_set_review import (
    SET_SEMANTIC_SELF_CHECKS_FIELD,
    set_semantic_review_is_current,
    set_semantic_review_signature,
)
from app.generation_v2.source_triage_workspace_contracts import TRIAGE_SEMANTIC_FIELD_LIMITS


class SourceTriageWorkspaceReviewHandlersMixin:
    async def save_semantic_review_self_check_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Persist a compact acknowledgement; semantic review belongs to the agent."""

        current = await workspace_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id")
            or self.current_set_id
            or (current.get("current_set") or {}).get("set_id")
            or ""
        )
        source_set = workspace_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        snapshot_id = set_semantic_review_signature(current, source_set)
        if (
            str(args.get("review_snapshot_id") or "") != snapshot_id
            or not set_semantic_review_is_current(current, source_set)
        ):
            return {
                "accepted": False,
                "error": "semantic_review_snapshot_not_current",
                "current_review_snapshot_id": snapshot_id,
                "next_action": (
                    "Read every page of the current semantic_review snapshot. If you change semantic state, "
                    "restart from cursor 0 of the new snapshot; otherwise acknowledge this exact snapshot."
                ),
            }
        explanation = str(args.get("readiness_explanation") or "").strip()
        field_error = semantic_field_error(
            {"semantic_review_conclusion": explanation},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["semantic_review_conclusion"],
        )
        if field_error:
            return field_error
        if not explanation:
            return {
                "accepted": False,
                "error": "readiness_explanation_required",
                "next_action": "Explain briefly why the current saved set is ready, then retry.",
            }
        checks = dict(current.get(SET_SEMANTIC_SELF_CHECKS_FIELD) or {})
        checks[set_id] = {
            "set_id": set_id,
            "contract_version": "v2.semantic_review_ack.1",
            "review_snapshot_id": snapshot_id,
            "readiness_explanation": explanation,
            "created_at": workspace_api._now(),
        }
        current[SET_SEMANTIC_SELF_CHECKS_FIELD] = checks
        await workspace_api.save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=current,
        )
        return {
            "accepted": True,
            "set_id": set_id,
            "review_snapshot_id": snapshot_id,
            "next_action": "Call finish_triage_source_set while this snapshot remains current.",
        }


__all__ = ["SourceTriageWorkspaceReviewHandlersMixin"]
