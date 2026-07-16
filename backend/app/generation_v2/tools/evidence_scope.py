from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.research_workspace import research_workflow_commit_advisory
from app.generation_v2.runtime_tool_outcomes import is_control_tool_output, successful_read_output
from app.generation_v2.source_triage_workspace_tracking import (
    blocked_read_control,
    completed_read_control,
    triage_workflow_read_control,
)
from app.generation_v2.tools.evidence_refs import (
    SourceRefContext,
    build_source_ref_context,
    build_source_ref_context_from_ids,
    resolve_source_ref,
    source_ref_for_id,
    version_ref_for_index,
)
from app.generation_v2.tools.evidence_scope_versions import (
    invalid_source_or_version_ref_error,
    source_version_id_from_ref,
    version_ref_for_version,
    versions_for_source,
)
from app.generation_v2.tools.evidence_source_map import build_source_inventory
from app.generation_v2.tools.evidence_tool_payloads import active_filter_list
from app.generation_v2.tools.evidence_utils import _dedupe, _readable_version_priority, _uuid_list, _uuid_or_none
from app.models.generation_v2 import SourceVersion


class EvidenceScopeMixin:
    def _scoped_allowed_source_refs(self) -> set[str] | None:
        triage_refs = getattr(self, "triage_allowed_source_refs", None)
        if triage_refs is not None:
            return set(triage_refs)
        research_refs = getattr(self, "research_allowed_source_refs", None)
        if research_refs is not None:
            return set(research_refs)
        return None

    async def _source_ref_context(self) -> SourceRefContext:
        cached = getattr(self, "_source_ref_context_cache", None)
        if cached is not None:
            return cached
        inventory = await build_source_inventory(self.db, self.run_view)
        context = build_source_ref_context(inventory)
        if not context.ref_by_source_id and getattr(self.run_view, "selected_source_ids", None):
            context = build_source_ref_context_from_ids([str(item) for item in self.run_view.selected_source_ids])
        setattr(self, "_source_ref_context_cache", context)
        return context

    async def _source_id_from_ref_or_id(self, value: str) -> str | None:
        context = await self._source_ref_context()
        return resolve_source_ref(value, context)

    async def _source_ids_from_refs_or_ids(self, values: list[str]) -> tuple[list[str], list[str]]:
        context = await self._source_ref_context()
        resolved: list[str] = []
        invalid: list[str] = []
        for value in values:
            source_id = resolve_source_ref(str(value), context)
            if source_id:
                resolved.append(source_id)
            else:
                invalid.append(str(value))
        return _dedupe(resolved), invalid

    async def _triage_allowed_source_ids(self) -> set[str] | None:
        allowed_refs = self._scoped_allowed_source_refs()
        if allowed_refs is None:
            return None
        source_ids, _ = await self._source_ids_from_refs_or_ids(sorted(str(item) for item in allowed_refs))
        return set(source_ids)

    async def _triage_scope_error_for_refs(self, refs: list[str]) -> dict[str, Any] | None:
        allowed_refs = self._scoped_allowed_source_refs()
        if allowed_refs is None:
            return None
        invalid = [str(ref) for ref in refs if str(ref) not in allowed_refs]
        if not invalid:
            return None
        is_research = getattr(self, "research_allowed_source_refs", None) is not None
        return {
            "accepted": False,
            "error": "source_refs_outside_current_research_job"
            if is_research
            else "source_refs_outside_current_triage_set",
            "message": (
                "This research job can only use source_refs from its current investigation job."
                if is_research
                else "This source-set agent can only use source_refs from its current source set."
            ),
            "invalid_source_refs": invalid,
            "allowed_source_refs": sorted(allowed_refs),
        }

    async def _triage_write_scope_error_for_refs(self, refs: list[str]) -> dict[str, Any] | None:
        repair_refs = getattr(self, "triage_write_allowed_source_refs", None)
        if repair_refs is None:
            return await self._triage_scope_error_for_refs(refs)
        allowed_refs = {str(item) for item in repair_refs}
        invalid_refs = [str(ref) for ref in refs if str(ref) not in allowed_refs]
        if refs and not invalid_refs:
            return None
        return {
            "accepted": False,
            "error": "map_write_outside_targeted_repair_scope",
            "message": "Targeted repair may write map records only for its explicit repair_source_refs.",
            "invalid_source_refs": invalid_refs or [str(ref) for ref in refs],
            "allowed_source_refs": sorted(allowed_refs),
        }

    async def _triage_scope_error_for_source_ids(self, source_ids: list[str]) -> dict[str, Any] | None:
        allowed_ids = await self._triage_allowed_source_ids()
        if allowed_ids is None:
            return None
        invalid = [str(source_id) for source_id in source_ids if str(source_id) not in allowed_ids]
        if not invalid:
            return None
        context = await self._source_ref_context()
        invalid_refs = [ref for source_id in invalid if (ref := source_ref_for_id(source_id, context)) != "S??"]
        is_research = getattr(self, "research_allowed_source_refs", None) is not None
        return {
            "accepted": False,
            "error": "sources_outside_current_research_job" if is_research else "sources_outside_current_triage_set",
            "message": (
                "This research job can only use sources from its current investigation job."
                if is_research
                else "This source-set agent can only use sources from its current source set."
            ),
            "invalid_source_refs": invalid_refs,
            "invalid_source_count": len(invalid),
        }

    async def _triage_read_guard_error(self) -> dict[str, Any] | None:
        if getattr(self, "research_current_job_id", None):
            advisory = await research_workflow_commit_advisory(
                self.db,
                generation_run_id=self.run.id,
                current_job_id=getattr(self, "research_current_job_id", None),
            )
            setattr(self, "_research_commit_advisory", advisory)
            return None
        control = await triage_workflow_read_control(
            self.db,
            generation_run_id=self.run.id,
            current_set_id=getattr(self, "triage_current_set_id", None),
        )
        setattr(self, "_triage_read_control", control)
        if control and control.get("must_persist_before_more_reads"):
            return blocked_read_control(control)
        return None

    async def _call_with_research_commit_advisory(self, handler, args: dict[str, Any]) -> dict[str, Any]:
        setattr(self, "_research_commit_advisory", None)
        setattr(self, "_triage_read_control", None)
        output = await handler(args)
        output = self._with_research_commit_advisory(output)
        return self._with_triage_read_control(output)

    def _with_triage_read_control(self, output: dict[str, Any]) -> dict[str, Any]:
        control = getattr(self, "_triage_read_control", None)
        if not control or not isinstance(output, dict) or is_control_tool_output(output):
            return output
        if not successful_read_output(output):
            return output
        return {**output, "read_control": completed_read_control(control)}

    def _with_research_commit_advisory(self, output: dict[str, Any]) -> dict[str, Any]:
        advisory = getattr(self, "_research_commit_advisory", None)
        if not advisory or not advisory.get("commit_recommended") or not isinstance(output, dict):
            return output
        if output.get("accepted") is False:
            return output
        if output.get("found") is False or output.get("error"):
            return output
        payload = dict(output)
        payload["commit_advisory"] = advisory
        return payload

    async def _source_ref_for_id(self, source_id: uuid.UUID | str) -> str:
        context = await self._source_ref_context()
        return source_ref_for_id(str(source_id), context)

    async def _version_ref_for_version(self, version: SourceVersion) -> str:
        return await version_ref_for_version(self, version)

    async def _source_version_id_from_ref(self, version_ref: str) -> str | None:
        return await source_version_id_from_ref(self, version_ref)

    async def _invalid_source_or_version_ref_error(
        self, *, source_ref: str = "", version_ref: str = ""
    ) -> dict[str, Any] | None:
        return await invalid_source_or_version_ref_error(self, source_ref=source_ref, version_ref=version_ref)

    async def _versions_for_source(self, source_id: str) -> list[SourceVersion]:
        return await versions_for_source(self, source_id)


__all__ = [
    "active_filter_list",
    "invalid_source_or_version_ref_error",
    "select",
    "source_version_id_from_ref",
    "version_ref_for_version",
    "version_ref_for_index",
    "versions_for_source",
    "_readable_version_priority",
    "_uuid_list",
    "_uuid_or_none",
]
