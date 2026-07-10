from __future__ import annotations

import json
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS,
    TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT,
    TRIAGE_CAPSULE_PAGE_MAX_LIMIT,
    TRIAGE_QUESTION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)


class SourceTriageWorkspacePlanningHandlersMixin:
    async def list_source_capsules_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = [str(item) for item in current.get("source_order") or []]
        limit = max(1, min(int(args.get("limit") or TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT), TRIAGE_CAPSULE_PAGE_MAX_LIMIT))
        cursor = str(args.get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0
        refs = source_order[offset : offset + limit]
        if self.evidence_toolset is not None:
            current = await public_api.ensure_capsules_for_refs(
                self.db,
                run=self.run,
                workspace=current,
                inventory=self.inventory,
                evidence_toolset=self.evidence_toolset,
                source_refs=refs,
            )
        capsules = current.get("source_capsules") or {}
        page_capsules: list[dict[str, Any]] = []
        for ref in refs:
            capsule = capsules.get(ref) or public_api._metadata_only_capsule(self.inventory, ref)
            candidate = [*page_capsules, capsule]
            encoded = json.dumps(
                public_api._capsule_page_payload(
                    source_count=len(source_order),
                    capsules=candidate,
                    next_cursor=str(offset + len(candidate)) if offset + len(candidate) < len(source_order) else "",
                ),
                ensure_ascii=False,
                default=str,
            )
            if page_capsules and len(encoded) > TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS:
                break
            page_capsules = candidate
        returned_count = len(page_capsules)
        return {
            **public_api._capsule_page_payload(
                source_count=len(source_order),
                capsules=page_capsules,
                next_cursor=str(offset + returned_count) if offset + returned_count < len(source_order) else "",
            ),
            "returned_count": returned_count,
            "page_limit": limit,
        }

    async def upsert_triage_source_set_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = set(str(item) for item in current.get("source_order") or [])
        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {"accepted": False, "error": "source_refs must exist in the triage workspace", "invalid_source_refs": invalid}
        if not source_refs:
            return {"accepted": False, "error": "upsert_triage_source_set requires at least one source_ref"}
        label = str(args.get("label") or "").strip()
        purpose = str(args.get("purpose") or "").strip()
        if not label or not purpose:
            return {"accepted": False, "error": "label and purpose are required"}
        rationale = str(args.get("rationale") or "").strip()
        priority = str(args.get("priority") or "normal").strip() or "normal"
        field_error = semantic_field_error(
            {"label": label, "purpose": purpose, "rationale": rationale, "priority": priority},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["label", "purpose", "rationale", "priority"],
        )
        if field_error:
            return field_error
        questions = [str(item).strip() for item in args.get("questions") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="questions",
            values=questions,
            max_items=TRIAGE_QUESTION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["question"],
        )
        if list_error:
            return list_error
        set_id = public_api._stable_set_id(str(args.get("set_id") or ""), label, len(current.get("source_sets") or []) + 1)
        source_set = {
            "set_id": set_id,
            "label": label,
            "purpose": purpose,
            "source_refs": source_refs,
            "status": "planned",
            "rationale": rationale,
            "questions": questions,
            "priority": priority,
            "findings_summary": "",
            "evidence_ids": [],
            "written_actions": [],
            "observations": [],
            "deferred_reason": "",
            "repair": bool(args.get("repair")),
            "updated_at": public_api._now(),
        }
        sets = [item for item in current.get("source_sets") or [] if item.get("set_id") != set_id]
        sets.append(source_set)
        current["source_sets"] = sets
        current["status"] = "planning"
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "source_count": len(source_refs), "planned_set_count": len(sets)}

    async def finalize_triage_workspace_plan_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = [str(item) for item in current.get("source_order") or []]
        source_sets = [dict(item) for item in current.get("source_sets") or []]
        covered = {ref for item in source_sets for ref in item.get("source_refs") or []}
        deferred = []
        for item in args.get("deferred_sources") or []:
            if isinstance(item, dict):
                refs = [str(ref) for ref in item.get("source_refs") or [] if str(ref).strip()]
                reason = str(item.get("reason") or "").strip()
            else:
                refs = [str(item)]
                reason = str(args.get("deferred_reason") or "").strip()
            for ref in refs:
                if ref in source_order:
                    deferred.append({"source_ref": ref, "reason": reason or "Deferred by triage planner."})
        deferred_refs = {item["source_ref"] for item in deferred}
        missing = [ref for ref in source_order if ref not in covered and ref not in deferred_refs]
        if not source_sets:
            return {"accepted": False, "error": "Cannot finalize triage workspace without source sets"}
        if missing:
            return {
                "accepted": False,
                "error": "Every source_ref must be assigned to a source set or explicitly deferred",
                "missing_source_refs": missing[:80],
            }
        for item in deferred:
            field_error = semantic_field_error(
                {"deferred_reason": item.get("reason") or ""},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["deferred_reason"],
            )
            if field_error:
                return field_error
        summary = str(args.get("summary") or "").strip()
        field_error = semantic_field_error({"plan_summary": summary}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["plan_summary"])
        if field_error:
            return field_error
        current["deferred_sources"] = public_api._merge_deferred(current.get("deferred_sources") or [], deferred)
        current["status"] = "planned"
        current["plan_summary"] = summary
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "status": "planned",
            "source_set_count": len(source_sets),
            "deferred_source_count": len(current["deferred_sources"]),
        }


__all__ = ["SourceTriageWorkspacePlanningHandlersMixin"]
