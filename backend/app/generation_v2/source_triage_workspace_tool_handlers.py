from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.schemas import SourceMap
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_PENDING_QUESTION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_workspace_planning_handlers import SourceTriageWorkspacePlanningHandlersMixin
from app.models.generation_run import GenerationRun


@dataclass
class SourceTriageWorkspaceToolHandlers(SourceTriageWorkspacePlanningHandlersMixin):
    db: AsyncSession
    run: GenerationRun
    workspace: dict[str, Any]
    inventory: SourceMap
    evidence_toolset: Any | None = None
    current_set_id: str | None = None

    async def record_triage_observation_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or "")
        source_refs = public_api._dedupe([str(item) for item in args.get("source_refs") or [] if str(item).strip()])
        if not set_id:
            return {"accepted": False, "error": "record_triage_observation requires a current set_id"}
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        invalid = [ref for ref in source_refs if ref not in set(source_set.get("source_refs") or [])]
        if invalid:
            return {"accepted": False, "error": "Observation source_refs must belong to the current source set", "invalid_source_refs": invalid}
        if not source_refs:
            return {"accepted": False, "error": "record_triage_observation requires at least one source_ref"}
        note = str(args.get("note") or "").strip()
        if not note:
            return {"accepted": False, "error": "note is required"}
        field_error = semantic_field_error({"note": note}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["note"])
        if field_error:
            return field_error
        pending_questions = [str(item).strip() for item in args.get("pending_questions") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="pending_questions",
            values=pending_questions,
            max_items=TRIAGE_PENDING_QUESTION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["pending_question"],
        )
        if list_error:
            return list_error
        evidence_ids = public_api._dedupe([str(item) for item in args.get("evidence_ids") or [] if str(item).strip()])
        if evidence_ids and self.evidence_toolset is not None:
            invalid_evidence_ids: list[str] = []
            allowed_refs = set(source_set.get("source_refs") or [])
            for evidence_id in evidence_ids:
                evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                if evidence is None:
                    invalid_evidence_ids.append(evidence_id)
                    continue
                evidence_ref = await self.evidence_toolset._source_ref_for_id(evidence.source_id)
                if evidence_ref not in allowed_refs:
                    invalid_evidence_ids.append(evidence_id)
            if invalid_evidence_ids:
                return {
                    "accepted": False,
                    "error": "observation evidence_ids must be observed evidence from the current source set",
                    "invalid_evidence_ids": invalid_evidence_ids,
                }
        observation = {
            "source_refs": source_refs,
            "evidence_ids": evidence_ids,
            "note": note,
            "pending_questions": pending_questions,
            "created_at": public_api._now(),
        }
        source_set.setdefault("observations", []).append(observation)
        source_set["evidence_ids"] = public_api._dedupe([*(source_set.get("evidence_ids") or []), *evidence_ids])
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "set_id": set_id,
            "observation_count": len(source_set.get("observations") or []),
            "evidence_count": len(evidence_ids),
        }

    async def finish_triage_source_set_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or "")
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        status = str(args.get("status") or "").strip()
        if status not in {"mapped", "deferred", "needs_repair"}:
            return {"accepted": False, "error": "status must be mapped, deferred, or needs_repair"}
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"accepted": False, "error": "summary is required"}
        if status == "mapped":
            has_semantic_state = bool(source_set.get("observations") or source_set.get("written_actions") or source_set.get("evidence_ids"))
            if not has_semantic_state:
                return {"accepted": False, "error": "mapped set requires at least one observation, write action, or evidence_id"}
        if status in {"deferred", "needs_repair"} and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred/needs_repair status"}
        deferred_reason = str(args.get("deferred_reason") or "").strip()
        deferred_sources = []
        for item in args.get("deferred_sources") or []:
            if not isinstance(item, dict):
                continue
            source_ref = str(item.get("source_ref") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if source_ref not in set(source_set.get("source_refs") or []):
                return {"accepted": False, "error": "deferred source_ref must belong to the current source set", "source_ref": source_ref}
            if not reason:
                return {"accepted": False, "error": "Each deferred source requires a reason", "source_ref": source_ref}
            reason_error = semantic_field_error(
                {"deferred_reason": reason}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["deferred_reason"]
            )
            if reason_error:
                return reason_error
            deferred_sources.append({"source_ref": source_ref, "reason": reason})
        if status == "mapped":
            covered_refs = set(public_api._source_set_covered_refs(source_set))
            deferred_refs = {item["source_ref"] for item in deferred_sources}
            unresolved_refs = [
                str(ref)
                for ref in source_set.get("source_refs") or []
                if str(ref) not in covered_refs and str(ref) not in deferred_refs
            ]
            if unresolved_refs:
                return {
                    "accepted": False,
                    "error": "Every source in a mapped set must be covered by a durable observation/write or explicitly deferred",
                    "unresolved_source_refs": unresolved_refs,
                }
            if not covered_refs:
                return {"accepted": False, "error": "Use deferred status when no source in the set was mapped"}
        field_error = semantic_field_error(
            {"findings_summary": summary, "deferred_reason": deferred_reason},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["findings_summary", "deferred_reason"],
        )
        if field_error:
            return field_error
        source_set["status"] = status
        source_set["findings_summary"] = summary
        source_set["deferred_reason"] = deferred_reason
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        processed = [item for item in current.get("processed_sets") or [] if item.get("set_id") != set_id]
        processed.append({"set_id": set_id, "status": status, "summary": source_set["findings_summary"], "updated_at": public_api._now()})
        current["processed_sets"] = processed
        current["current_set"] = None
        if status == "mapped":
            current["mapped_sources"] = public_api._dedupe(
                [*(current.get("mapped_sources") or []), *public_api._source_set_covered_refs(source_set)]
            )
            current["deferred_sources"] = public_api._merge_deferred(
                current.get("deferred_sources") or [], deferred_sources
            )
        if status == "deferred":
            current["deferred_sources"] = public_api._merge_deferred(
                current.get("deferred_sources") or [],
                [{"source_ref": ref, "reason": source_set["deferred_reason"] or summary} for ref in source_set.get("source_refs") or []],
            )
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "status": status, "processed_set_count": len(processed)}


__all__ = ["SourceTriageWorkspaceToolHandlers"]
