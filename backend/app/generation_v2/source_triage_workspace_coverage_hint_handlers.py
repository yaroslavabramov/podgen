from __future__ import annotations

import hashlib
import json
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_workspace_state import invalidate_coverage_syntheses
from app.generation_v2.source_triage_question_references import known_question_id_error


class SourceTriageWorkspaceCoverageHintHandlersMixin:
    async def save_coverage_hint_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        current.setdefault("coverage_hints", [])
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}

        source_ref = str(args.get("source_ref") or "").strip()
        if source_ref not in {str(item) for item in source_set.get("source_refs") or []}:
            return {
                "accepted": False,
                "error": "coverage_hint_source_ref_must_belong_to_current_set",
                "source_ref": source_ref,
                "allowed_source_refs": [str(item) for item in source_set.get("source_refs") or []],
                "next_action": "Copy one source_ref from set_overview/source_memory and retry.",
            }

        question_id = str(args.get("question_id") or "").strip()
        question = next(
            (
                item
                for item in current.get("coverage_questions") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            None,
        )
        if question is None:
            return known_question_id_error(
                current,
                question_id,
                error="coverage_hint_question_id_must_be_persisted",
            )

        requirement_ids = public_api._dedupe(
            [str(item) for item in args.get("requirement_ids") or [] if str(item).strip()]
        )
        known_requirement_ids = {
            str(item.get("requirement_id") or "")
            for item in question.get("answer_requirements") or []
            if isinstance(item, dict) and str(item.get("requirement_id") or "")
        }
        unknown_requirement_ids = sorted(set(requirement_ids) - known_requirement_ids)
        if unknown_requirement_ids:
            return {
                "accepted": False,
                "error": "coverage_hint_requirement_ids_must_belong_to_question",
                "question_id": question_id,
                "unknown_requirement_ids": unknown_requirement_ids,
                "allowed_requirement_ids": sorted(known_requirement_ids),
            }

        candidate_finding_ids = public_api._dedupe(
            [str(item) for item in args.get("candidate_finding_ids") or [] if str(item).strip()]
        )
        findings_by_id = {
            str(item.get("finding_id") or ""): item
            for item in current.get("source_findings") or []
            if isinstance(item, dict)
            and str(item.get("set_id") or "") == set_id
            and str(item.get("source_ref") or "") == source_ref
            and str(item.get("finding_id") or "")
        }
        unknown_finding_ids = sorted(set(candidate_finding_ids) - set(findings_by_id))
        if unknown_finding_ids:
            return {
                "accepted": False,
                "error": "coverage_hint_findings_must_belong_to_current_source_analysis",
                "source_ref": source_ref,
                "unknown_finding_ids": unknown_finding_ids,
                "allowed_finding_ids": sorted(findings_by_id),
            }

        candidate_evidence_ids = public_api._dedupe(
            [str(item) for item in args.get("candidate_evidence_ids") or [] if str(item).strip()]
        )
        allowed_evidence_ids = _source_evidence_ids(current, set_id=set_id, source_ref=source_ref)
        unknown_evidence_ids = sorted(set(candidate_evidence_ids) - allowed_evidence_ids)
        if unknown_evidence_ids:
            return {
                "accepted": False,
                "error": "coverage_hint_evidence_must_belong_to_current_source_analysis",
                "source_ref": source_ref,
                "unknown_evidence_ids": unknown_evidence_ids,
                "allowed_evidence_ids": sorted(allowed_evidence_ids),
            }

        observed_gap = str(args.get("observed_gap") or "").strip()
        conflict_signal = str(args.get("conflict_signal") or "").strip()
        notes = [str(item).strip() for item in args.get("notes") or [] if str(item).strip()]
        if not candidate_finding_ids and not candidate_evidence_ids and not observed_gap and not conflict_signal and not notes:
            return {
                "accepted": False,
                "error": "coverage_hint_requires_material_or_gap",
                "next_action": (
                    "Attach candidate finding/evidence ids, or explain the observed source-specific gap/conflict."
                ),
            }

        field_error = semantic_field_error(
            {
                "coverage_detail": observed_gap,
                "coverage_synthesis_detail": conflict_signal,
            },
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["coverage_detail", "coverage_synthesis_detail"],
        )
        if field_error:
            return field_error
        notes_error = semantic_list_error(
            field="coverage_hint_notes",
            values=notes,
            max_items=TRIAGE_LIMITATION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_hint_note"],
        )
        if notes_error:
            return notes_error

        supplied_hint_id = str(args.get("hint_id") or "").strip()
        hint_id = supplied_hint_id or _stable_hint_id(
            set_id=set_id,
            source_ref=source_ref,
            question_id=question_id,
            requirement_ids=requirement_ids,
            candidate_finding_ids=candidate_finding_ids,
            candidate_evidence_ids=candidate_evidence_ids,
            observed_gap=observed_gap,
            conflict_signal=conflict_signal,
        )
        previous = next(
            (
                item
                for item in current.get("coverage_hints") or []
                if isinstance(item, dict) and str(item.get("hint_id") or "") == hint_id
            ),
            None,
        )
        if previous is not None and (
            str(previous.get("set_id") or "") != set_id or str(previous.get("source_ref") or "") != source_ref
        ):
            return {
                "accepted": False,
                "error": "coverage_hint_id_belongs_to_different_source",
                "hint_id": hint_id,
                "existing_set_id": previous.get("set_id"),
                "existing_source_ref": previous.get("source_ref"),
            }

        hint = {
            "hint_id": hint_id,
            "set_id": set_id,
            "source_ref": source_ref,
            "question_id": question_id,
            "requirement_ids": requirement_ids,
            "candidate_finding_ids": candidate_finding_ids,
            "candidate_evidence_ids": candidate_evidence_ids,
            "observed_gap": observed_gap,
            "conflict_signal": conflict_signal,
            "notes": notes,
            "created_at": (previous or {}).get("created_at") or public_api._now(),
            "updated_at": public_api._now(),
        }
        changed = _semantic_json(previous or {}) != _semantic_json(hint)
        current["coverage_hints"] = [
            item
            for item in current.get("coverage_hints") or []
            if not isinstance(item, dict) or str(item.get("hint_id") or "") != hint_id
        ]
        current["coverage_hints"].append(hint)
        if changed:
            current["triage_semantic_revision"] = int(current.get("triage_semantic_revision") or 0) + 1
            invalidate_coverage_syntheses(current)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "hint_id": hint_id,
            "set_id": set_id,
            "source_ref": source_ref,
            "question_id": question_id,
            "changed": changed,
            "next_action": "Continue source mapping; coverage hints are not question outcomes.",
        }


def _source_evidence_ids(workspace: dict[str, Any], *, set_id: str, source_ref: str) -> set[str]:
    evidence_ids: set[str] = set()
    for assessment in workspace.get("source_assessments") or []:
        if (
            isinstance(assessment, dict)
            and str(assessment.get("set_id") or "") == set_id
            and str(assessment.get("source_ref") or "") == source_ref
        ):
            evidence_ids.update(str(item) for item in assessment.get("evidence_ids") or [] if str(item).strip())
    for finding in workspace.get("source_findings") or []:
        if (
            isinstance(finding, dict)
            and str(finding.get("set_id") or "") == set_id
            and str(finding.get("source_ref") or "") == source_ref
        ):
            evidence_ids.update(str(item) for item in finding.get("evidence_ids") or [] if str(item).strip())
    return evidence_ids


def retire_coverage_hints_for_source_analysis(
    workspace: dict[str, Any],
    *,
    set_id: str,
    source_ref: str,
    reason: str,
) -> list[str]:
    """Retire active hints tied to a source whose semantic analysis changed."""

    retired_ids: list[str] = []
    kept: list[dict[str, Any]] = []
    retired = list(workspace.get("retired_coverage_hints") or [])
    now = workspace_api._now()
    for hint in workspace.get("coverage_hints") or []:
        if not (
            isinstance(hint, dict)
            and str(hint.get("set_id") or "") == set_id
            and str(hint.get("source_ref") or "") == source_ref
        ):
            kept.append(hint)
            continue
        retired_hint = dict(hint)
        retired_hint["retired_at"] = now
        retired_hint["retired_reason"] = reason
        retired.append(retired_hint)
        retired_ids.append(str(hint.get("hint_id") or ""))
    workspace["coverage_hints"] = kept
    if retired_ids:
        workspace["retired_coverage_hints"] = retired
    return retired_ids


def _stable_hint_id(
    *,
    set_id: str,
    source_ref: str,
    question_id: str,
    requirement_ids: list[str],
    candidate_finding_ids: list[str],
    candidate_evidence_ids: list[str],
    observed_gap: str,
    conflict_signal: str,
) -> str:
    payload = {
        "set_id": set_id,
        "source_ref": source_ref,
        "question_id": question_id,
        "requirement_ids": requirement_ids,
        "candidate_finding_ids": candidate_finding_ids,
        "candidate_evidence_ids": candidate_evidence_ids,
        "observed_gap": observed_gap,
        "conflict_signal": conflict_signal,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"hint:{set_id}:{source_ref}:{question_id}:{digest}"


def _semantic_json(value: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: item
            for key, item in value.items()
            if key not in {"created_at", "updated_at"}
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


__all__ = [
    "SourceTriageWorkspaceCoverageHintHandlersMixin",
    "retire_coverage_hints_for_source_analysis",
]
