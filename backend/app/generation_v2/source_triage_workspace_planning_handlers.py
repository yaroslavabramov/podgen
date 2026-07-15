from __future__ import annotations

import json
import hashlib
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS,
    TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT,
    TRIAGE_CAPSULE_PAGE_MAX_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)


class SourceTriageWorkspacePlanningHandlersMixin:
    async def upsert_coverage_question_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        question = str(args.get("question") or "").strip()
        if not question:
            return {"accepted": False, "error": "question is required"}
        field_error = semantic_field_error(
            {
                "question": question,
                "question_rationale": str(args.get("rationale") or "").strip(),
            },
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["question", "question_rationale"],
        )
        if field_error:
            return field_error
        kind = str(args.get("kind") or "other").strip()
        importance = str(args.get("importance") or "supporting").strip()
        allowed_kinds = {
            "context",
            "origin",
            "mechanism",
            "cause",
            "change",
            "impact",
            "exception",
            "verification",
            "other",
        }
        if kind not in allowed_kinds:
            return {"accepted": False, "error": "invalid coverage question kind"}
        if importance not in {"critical", "supporting", "exploratory"}:
            return {"accepted": False, "error": "invalid coverage question importance"}
        requested_id = str(args.get("question_id") or "").strip()
        if requested_id:
            question_id = public_api._stable_set_id(requested_id, question, 1)
        else:
            digest = hashlib.sha256(question.casefold().encode("utf-8")).hexdigest()[:10]
            question_id = f"q_{digest}"
        parent_question_id = str(args.get("parent_question_id") or "").strip() or None
        existing_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        if parent_question_id and parent_question_id != question_id and parent_question_id not in existing_ids:
            return {"accepted": False, "error": "parent_question_id must reference an existing question"}
        item = {
            "question_id": question_id,
            "question": question,
            "kind": kind,
            "importance": importance,
            "rationale": str(args.get("rationale") or "").strip(),
            "parent_question_id": parent_question_id,
            "origin": "planned",
        }
        current["coverage_questions"] = [
            item if existing.get("question_id") == question_id else existing
            for existing in current.get("coverage_questions") or []
        ]
        if question_id not in existing_ids:
            current["coverage_questions"].append(item)
        current["status"] = "planning"
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "question_id": question_id,
            "coverage_question_count": len(current["coverage_questions"]),
        }

    async def list_source_capsules_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = [str(item) for item in current.get("source_order") or []]
        requested_refs = public_api._dedupe(
            [str(item) for item in args.get("source_refs") or [] if str(item).strip()]
        )
        invalid_refs = [ref for ref in requested_refs if ref not in source_order]
        if invalid_refs:
            return {
                "accepted": False,
                "error": "source_refs must exist in the triage workspace",
                "invalid_source_refs": invalid_refs,
            }
        if len(requested_refs) > 1:
            return {"accepted": False, "error": "request one source_ref at a time"}
        limit = max(1, min(int(args.get("limit") or TRIAGE_CAPSULE_PAGE_DEFAULT_LIMIT), TRIAGE_CAPSULE_PAGE_MAX_LIMIT))
        cursor = str(args.get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0
        refs = requested_refs or source_order[offset : offset + limit]
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
                    next_cursor=(
                        ""
                        if requested_refs
                        else str(offset + len(candidate)) if offset + len(candidate) < len(source_order) else ""
                    ),
                ),
                ensure_ascii=False,
                default=str,
            )
            if page_capsules and len(encoded) > TRIAGE_CAPSULE_OUTPUT_TARGET_CHARS:
                break
            page_capsules = candidate
        returned_count = len(page_capsules)
        next_cursor = (
            ""
            if requested_refs
            else str(offset + returned_count) if offset + returned_count < len(source_order) else ""
        )
        return {
            **public_api._capsule_page_payload(
                source_count=len(source_order),
                capsules=page_capsules,
                next_cursor=next_cursor,
            ),
            "returned_count": returned_count,
            "page_limit": limit,
            "requested_source_refs": requested_refs,
        }

    async def upsert_triage_source_set_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        source_order = set(str(item) for item in current.get("source_order") or [])
        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        invalid = [ref for ref in source_refs if ref not in source_order]
        if invalid:
            return {
                "accepted": False,
                "error": "source_refs must exist in the triage workspace",
                "invalid_source_refs": invalid,
            }
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
        question_ids = public_api._dedupe([str(item) for item in args.get("question_ids") or [] if str(item).strip()])
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        unknown_question_ids = sorted(set(question_ids) - known_question_ids)
        if unknown_question_ids:
            return {
                "accepted": False,
                "error": "question_ids must reference persisted coverage questions",
                "unknown_question_ids": unknown_question_ids,
            }
        if not question_ids:
            return {
                "accepted": False,
                "error": "Every source set must be attached to at least one persisted coverage question",
            }
        set_id = public_api._stable_set_id(
            str(args.get("set_id") or ""), label, len(current.get("source_sets") or []) + 1
        )
        source_set = {
            "set_id": set_id,
            "label": label,
            "purpose": purpose,
            "source_refs": source_refs,
            "status": "planned",
            "rationale": rationale,
            "question_ids": question_ids,
            "planned_question_ids": question_ids,
            "cross_question_ids": [],
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
        if args.get("deferred_sources"):
            return {
                "accepted": False,
                "error": "The planner cannot defer sources; assign every source to exactly one set",
            }
        ref_counts = {ref: sum(ref in (item.get("source_refs") or []) for item in source_sets) for ref in source_order}
        duplicate_refs = sorted(ref for ref, count in ref_counts.items() if count > 1)
        missing = [ref for ref, count in ref_counts.items() if count == 0]
        if not source_sets:
            return {"accepted": False, "error": "Cannot finalize triage workspace without source sets"}
        if duplicate_refs:
            return {
                "accepted": False,
                "error": "Every source_ref must belong to exactly one source set",
                "duplicate_source_refs": duplicate_refs,
            }
        if missing:
            return {
                "accepted": False,
                "error": "Every source_ref must be assigned to exactly one source set",
                "missing_source_refs": missing,
            }
        questions = [item for item in current.get("coverage_questions") or [] if isinstance(item, dict)]
        if not questions:
            return {"accepted": False, "error": "Persist at least one coverage question before finalizing"}
        known_question_ids = {str(item.get("question_id") or "") for item in questions}
        referenced_question_ids = {
            str(question_id) for item in source_sets for question_id in item.get("question_ids") or []
        }
        unknown_question_ids = sorted(referenced_question_ids - known_question_ids)
        unassigned_question_ids = sorted(known_question_ids - referenced_question_ids)
        sets_without_questions = [
            str(item.get("set_id") or "") for item in source_sets if not (item.get("question_ids") or [])
        ]
        if unknown_question_ids or unassigned_question_ids or sets_without_questions:
            return {
                "accepted": False,
                "error": "Every coverage question and every source set must be connected",
                "unknown_question_ids": unknown_question_ids,
                "unassigned_question_ids": unassigned_question_ids,
                "source_sets_without_question_ids": sets_without_questions,
            }
        summary = str(args.get("summary") or "").strip()
        field_error = semantic_field_error({"plan_summary": summary}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["plan_summary"])
        if field_error:
            return field_error
        current["status"] = "planned"
        current["plan_summary"] = summary
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "status": "planned",
            "source_set_count": len(source_sets),
            "coverage_question_count": len(questions),
        }


__all__ = ["SourceTriageWorkspacePlanningHandlersMixin"]
