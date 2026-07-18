from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.persistence_contracts import semantic_field_error, semantic_list_error
from app.generation_v2.schemas import SourceMap
from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.source_triage_workspace_contracts import (
    TRIAGE_EDITORIAL_FUNCTION_LIMIT,
    TRIAGE_LIMITATION_LIMIT,
    TRIAGE_SEMANTIC_FIELD_LIMITS,
)
from app.generation_v2.source_triage_lineage import coverage_synthesis_lineage
from app.generation_v2.source_triage_workspace_planning_handlers import SourceTriageWorkspacePlanningHandlersMixin
from app.generation_v2.source_triage_workspace_state import (
    source_read_receipt_is_delivered,
    SOURCE_SET_KIND_COVERAGE,
    SOURCE_SET_KIND_ORIENTATION,
    invalidate_coverage_syntheses,
)
from app.generation_v2.source_triage_workspace_tracking import clear_pending_exact_read
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.generation_v2.tools.evidence_source_text import source_version_text
from app.generation_v2.tools.evidence_utils import _unsupported_model_read_selected_as_support
from app.ingestion.web_quality import assess_web_fetch_health
from app.models.generation_run import GenerationRun


_READING_NOTE_SCALAR_MAX_CHARS = MODEL_TOOL_OUTPUT_MAX_CHARS - 1_200


def _pending_set_work(workspace: dict[str, Any], source_set: dict[str, Any]) -> dict[str, Any]:
    set_id = str(source_set.get("set_id") or "")
    assessed_refs = {
        str(item.get("source_ref") or "")
        for item in workspace.get("source_assessments") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("processing_status") or "resolved") == "resolved"
        and (item.get("finding_ids") or str(item.get("no_findings_reason") or "").strip())
    }
    completed_question_ids = {
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and str(item.get("status") or "") in {"covered", "partial", "missing", "conflicted", "deferred"}
        and not bool(item.get("is_stale"))
    }
    required_question_ids = [str(item) for item in source_set.get("question_ids") or []]
    stale_question_ids = [
        str(item.get("question_id") or "")
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict)
        and str(item.get("set_id") or "") == set_id
        and bool(item.get("is_stale"))
    ]
    set_findings = [
        item
        for item in workspace.get("source_findings") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_outcomes = [
        item
        for item in workspace.get("coverage_outcomes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    set_reading_notes = [
        item
        for item in workspace.get("reading_notes") or []
        if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
    ]
    return {
        "pending_source_refs": [
            str(item) for item in source_set.get("source_refs") or [] if str(item) not in assessed_refs
        ],
        "required_question_ids": required_question_ids,
        "missing_outcome_question_ids": [
            question_id for question_id in required_question_ids if question_id not in completed_question_ids
        ],
        "stale_outcome_question_ids": workspace_api._dedupe(stale_question_ids),
        "saved_state": {
            "source_refs": sorted(assessed_refs),
            "finding_ids": sorted(
                str(item.get("finding_id") or "")
                for item in set_findings
                if str(item.get("finding_id") or "")
            ),
            "outcome_question_ids": sorted(
                str(item.get("question_id") or "")
                for item in set_outcomes
                if str(item.get("question_id") or "")
            ),
            "reading_note_ids": sorted(
                str(item.get("note_id") or "")
                for item in set_reading_notes
                if str(item.get("note_id") or "")
            ),
            "reading_note_count": len(set_reading_notes),
            "source_analysis_count": len(assessed_refs),
            "finding_count": len(set_findings),
            "outcome_count": len(set_outcomes),
        },
    }


@dataclass
class SourceTriageWorkspaceToolHandlers(SourceTriageWorkspacePlanningHandlersMixin):
    db: AsyncSession
    run: GenerationRun
    workspace: dict[str, Any]
    inventory: SourceMap
    evidence_toolset: Any | None = None
    current_set_id: str | None = None

    async def save_reading_note_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Save semantic working memory only for ranges actually returned to this set agent."""

        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {
                "accepted": False,
                "error": "Unknown current source triage set",
                "set_id": set_id,
            }

        source_ref = str(args.get("source_ref") or "").strip()
        if source_ref not in {str(value) for value in source_set.get("source_refs") or []}:
            return {
                "accepted": False,
                "error": "source_ref must belong to the current source set",
                "source_ref": source_ref,
                "set_id": set_id,
            }
        source_id = resolve_source_ref(source_ref, build_source_ref_context(self.inventory))
        if not source_id:
            return {"accepted": False, "error": "Unknown source_ref", "source_ref": source_ref}

        raw_receipt_refs = args.get("receipt_refs")
        if not isinstance(raw_receipt_refs, list):
            return {"accepted": False, "error": "receipt_refs must be an array"}
        receipt_refs = [str(value or "").strip() for value in raw_receipt_refs]
        if not receipt_refs or any(not value for value in receipt_refs):
            return {
                "accepted": False,
                "error": "At least one non-empty read_source receipt_ref is required",
            }
        if len(set(receipt_refs)) != len(receipt_refs):
            return {"accepted": False, "error": "receipt_refs must be unique"}

        receipts_by_ref = {
            str(item.get("receipt_ref") or ""): item
            for item in current.get("source_read_receipts") or []
            if isinstance(item, dict) and str(item.get("receipt_ref") or "")
        }
        unknown_receipt_refs = [value for value in receipt_refs if value not in receipts_by_ref]
        if unknown_receipt_refs:
            return {
                "accepted": False,
                "error": (
                    "reading note may use only receipts previously returned by "
                    "read_source/search_source/open_source_file"
                ),
                "unknown_receipt_refs": unknown_receipt_refs,
            }

        inventory_card = next(
            (card for card in self.inventory.source_cards if str(card.source_id) == source_id),
            None,
        )
        current_version_ids = {
            str(value) for value in (getattr(inventory_card, "source_version_ids", []) or [])
        }
        invalid_receipt_refs: list[str] = []
        for receipt_ref in receipt_refs:
            receipt = receipts_by_ref[receipt_ref]
            read_kind = str(receipt.get("read_kind") or "")
            if not source_read_receipt_is_delivered(receipt):
                invalid_receipt_refs.append(receipt_ref)
                continue
            identity_invalid = (
                str(receipt.get("set_id") or "") != set_id
                or str(receipt.get("source_ref") or "") != source_ref
                or str(receipt.get("source_version_id") or "") not in current_version_ids
            )
            if identity_invalid:
                invalid_receipt_refs.append(receipt_ref)
                continue
            version = None
            if self.evidence_toolset is not None:
                version = await self.evidence_toolset._resolve_source_version(
                    source_id=source_id,
                    source_version_id=str(receipt.get("source_version_id") or ""),
                    source_ref=source_ref,
                    version_ref="",
                )
            if version is None or str(version.id) != str(receipt.get("source_version_id") or ""):
                invalid_receipt_refs.append(receipt_ref)
                continue
            # A native attachment means the original immutable representation was
            # delivered to this same agent. It is valid notebook provenance, but it
            # deliberately contributes no character-range or EOF claim.
            if read_kind == "native_file_attachment":
                try:
                    attachment_start = int(receipt.get("start_offset"))
                    attachment_end = int(receipt.get("end_offset"))
                except (TypeError, ValueError):
                    invalid_receipt_refs.append(receipt_ref)
                    continue
                pinned_hash = str(getattr(version, "content_hash", None) or "").strip().lower()
                receipt_hash = str(receipt.get("content_sha256") or "").strip().lower()
                if (
                    attachment_start != 0
                    or attachment_end != 0
                    or not pinned_hash
                    or receipt_hash != pinned_hash
                ):
                    invalid_receipt_refs.append(receipt_ref)
                continue
            try:
                start_offset = int(receipt.get("start_offset"))
                end_offset = int(receipt.get("end_offset"))
                total_chars = int(receipt.get("total_chars"))
            except (TypeError, ValueError):
                invalid_receipt_refs.append(receipt_ref)
                continue
            cached_text_getter = getattr(self.evidence_toolset, "_cached_source_version_text", None)
            pinned_text = (
                str(cached_text_getter(version))
                if callable(cached_text_getter)
                else source_version_text(version)
            )
            pinned_text_hash = hashlib.sha256(pinned_text.encode("utf-8")).hexdigest()
            if (
                str(receipt.get("content_sha256") or "") != pinned_text_hash
                or start_offset < 0
                or end_offset <= start_offset
                or total_chars != len(pinned_text)
                or end_offset > len(pinned_text)
            ):
                invalid_receipt_refs.append(receipt_ref)
        if invalid_receipt_refs:
            return {
                "accepted": False,
                "error": "reading note receipt does not belong to this source, set, or pinned source version",
                "invalid_receipt_refs": invalid_receipt_refs,
            }

        raw_question_links = args.get("question_links")
        if not isinstance(raw_question_links, list):
            return {"accepted": False, "error": "question_links must be an array"}
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        }
        question_links: list[dict[str, str]] = []
        unknown_question_ids: list[str] = []
        for item in raw_question_links:
            if not isinstance(item, dict):
                return {"accepted": False, "error": "question_links items must be objects"}
            question_id = str(item.get("question_id") or "").strip()
            if question_id not in known_question_ids:
                unknown_question_ids.append(question_id)
            question_links.append(
                {
                    "question_id": question_id,
                    "connection": str(item.get("connection") or ""),
                }
            )
        if unknown_question_ids:
            return {
                "accepted": False,
                "error": "reading note question_links must reference current persisted questions",
                "unknown_question_ids": sorted(set(unknown_question_ids)),
            }

        doubts = args.get("doubts")
        next_checks = args.get("next_checks")
        if not isinstance(doubts, list) or not isinstance(next_checks, list):
            return {
                "accepted": False,
                "error": "doubts and next_checks must be arrays",
            }
        semantic_scalars = [
            (field, None, str(args.get(field) or ""))
            for field in (
                "what_was_read",
                "what_was_established",
                "boundaries",
                "unread_regions",
                "why_unread_is_safe",
            )
        ]
        semantic_scalars.extend(
            ("question_links.connection", index, str(link.get("connection") or ""))
            for index, link in enumerate(question_links)
        )
        semantic_scalars.extend(
            (field, index, str(value))
            for field, values in (("doubts", doubts), ("next_checks", next_checks))
            for index, value in enumerate(values)
        )
        oversized = next(
            (
                (field, index, len(value))
                for field, index, value in semantic_scalars
                if len(value) > _READING_NOTE_SCALAR_MAX_CHARS
            ),
            None,
        )
        if oversized is not None:
            field, index, actual_chars = oversized
            return {
                "accepted": False,
                "error": "reading_note_field_exceeds_model_read_contract",
                "field": field,
                **({"index": index} if index is not None else {}),
                "actual_chars": actual_chars,
                "max_chars": _READING_NOTE_SCALAR_MAX_CHARS,
                "next_action": (
                    "Split the material across multiple reading notes with distinct note_id values; this field "
                    "was not persisted or truncated."
                ),
            }
        requested_note_id = str(args.get("note_id") or "").strip()
        note_id = requested_note_id or (
            "reading_note:"
            + hashlib.sha256(
                "|".join([set_id, source_ref, *receipt_refs]).encode("utf-8")
            ).hexdigest()[:24]
        )
        existing_note = next(
            (
                item
                for item in current.get("reading_notes") or []
                if isinstance(item, dict) and str(item.get("note_id") or "") == note_id
            ),
            None,
        )
        if existing_note is not None and (
            str(existing_note.get("set_id") or "") != set_id
            or str(existing_note.get("source_ref") or "") != source_ref
        ):
            return {
                "accepted": False,
                "error": "note_id belongs to another source or set",
                "note_id": note_id,
            }

        now = public_api._now()
        note = {
            "note_id": note_id,
            "set_id": set_id,
            "source_ref": source_ref,
            "receipt_refs": receipt_refs,
            # Semantic fields are persisted verbatim. They intentionally have no
            # model-facing preview limit or persistence truncation here.
            "what_was_read": str(args.get("what_was_read") or ""),
            "what_was_established": str(args.get("what_was_established") or ""),
            "boundaries": str(args.get("boundaries") or ""),
            "unread_regions": str(args.get("unread_regions") or ""),
            "why_unread_is_safe": str(args.get("why_unread_is_safe") or ""),
            "question_links": question_links,
            "doubts": [str(value) for value in doubts],
            "next_checks": [str(value) for value in next_checks],
            "created_at": (existing_note or {}).get("created_at") or now,
            "updated_at": now,
        }
        current["reading_notes"] = [
            item
            for item in current.get("reading_notes") or []
            if not (isinstance(item, dict) and str(item.get("note_id") or "") == note_id)
        ]
        current["reading_notes"].append(note)
        await public_api.save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=current,
        )
        return {
            "accepted": True,
            "note_id": note_id,
            "source_ref": source_ref,
            "receipt_refs": receipt_refs,
            **_pending_set_work(current, source_set),
        }

    async def upsert_source_assessment_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        source_ref = str(args.get("source_ref") or "").strip()
        if source_ref not in set(source_set.get("source_refs") or []):
            return {
                "accepted": False,
                "error": "source_ref must belong to the current source set",
                "source_ref": source_ref,
            }
        if source_set.get("repair_targeted") and source_ref not in {
            str(item) for item in source_set.get("repair_source_refs") or []
        }:
            return {
                "accepted": False,
                "error": "source assessment is outside the targeted repair scope",
                "source_ref": source_ref,
                "allowed_source_refs": list(source_set.get("repair_source_refs") or []),
            }
        source_id = resolve_source_ref(source_ref, build_source_ref_context(self.inventory))
        if not source_id:
            return {"accepted": False, "error": "Unknown source_ref", "source_ref": source_ref}

        enum_fields = {
            "processing_status": {"pending", "in_progress", "resolved", "needs_repair"},
            "representation_status": {
                "unchecked",
                "readable",
                "partial",
                "not_target_content",
                "unreadable",
            },
            "relevance": {"core", "supporting", "contextual", "out_of_scope", "unknown"},
            "authority": {"primary", "official", "expert", "secondary", "tertiary", "weak", "unknown"},
            "uniqueness": {"unique", "complementary", "derivative", "duplicate", "unknown"},
            "disposition": {"anchor", "supporting", "background", "weak", "unusable", "deferred"},
        }
        values = {
            "processing_status": str(args.get("processing_status") or "resolved"),
            "representation_status": str(args.get("representation_status") or "unchecked"),
            "relevance": str(args.get("relevance") or "unknown"),
            "authority": str(args.get("authority") or "unknown"),
            "uniqueness": str(args.get("uniqueness") or "unknown"),
            "disposition": str(args.get("disposition") or "background"),
        }
        for field, allowed in enum_fields.items():
            if values[field] not in allowed:
                return {"accepted": False, "error": f"invalid {field}", "value": values[field]}
        if values["processing_status"] in {"pending", "in_progress"}:
            return {
                "accepted": False,
                "error": "A persisted source assessment must be resolved or explicitly needs_repair",
            }
        if values["processing_status"] == "resolved" and values["representation_status"] == "unchecked":
            return {
                "accepted": False,
                "error": "A resolved source assessment must check the available representation",
            }

        reason_fields = {
            "relevance_reason": str(args.get("relevance_reason") or "").strip(),
            "authority_reason": str(args.get("authority_reason") or "").strip(),
            "uniqueness_reason": str(args.get("uniqueness_reason") or "").strip(),
            "representation_reason": str(args.get("representation_reason") or "").strip(),
            "disposition_reason": str(args.get("disposition_reason") or "").strip(),
        }
        reason_error = semantic_field_error(
            {key: value for key, value in reason_fields.items()},
            {
                "relevance_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "authority_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "uniqueness_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "representation_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
                "disposition_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_reason"],
            },
            list(reason_fields),
        )
        if reason_error:
            return reason_error
        if values["representation_status"] in {
            "partial",
            "not_target_content",
            "unreadable",
        } and not reason_fields["representation_reason"]:
            return {
                "accepted": False,
                "error": (
                    "partial/not_target_content/unreadable representation requires representation_reason"
                ),
            }
        if values["disposition"] in {"weak", "unusable", "deferred"} and not reason_fields[
            "disposition_reason"
        ]:
            return {
                "accepted": False,
                "error": "weak/unusable/deferred disposition requires disposition_reason",
            }
        if values["representation_status"] in {
            "not_target_content",
            "unreadable",
        } and values["disposition"] != "deferred":
            return {
                "accepted": False,
                "error": (
                    "A not_target_content or unreadable representation must use disposition=deferred; "
                    "it does not prove source value"
                ),
            }
        if values["disposition"] == "deferred" and values["representation_status"] not in {
            "partial",
            "not_target_content",
            "unreadable",
        }:
            return {
                "accepted": False,
                "error": (
                    "disposition=deferred requires a checked partial, not_target_content, or unreadable "
                    "representation"
                ),
            }
        if values["representation_status"] in {"not_target_content", "unreadable"}:
            if values["processing_status"] != "resolved":
                return {
                    "accepted": False,
                    "error": (
                        "A checked unavailable representation is a completed diagnosis and requires "
                        "processing_status=resolved"
                    ),
                }
            non_unknown_axes = [
                field
                for field in ("relevance", "authority", "uniqueness")
                if values[field] != "unknown"
            ]
            if non_unknown_axes:
                return {
                    "accepted": False,
                    "error": (
                        "Unavailable target content cannot establish relevance, authority, or uniqueness; "
                        "all three judgments must be unknown"
                    ),
                    "invalid_fields": non_unknown_axes,
                }
            asserted_reason_fields = [
                field
                for field in ("relevance_reason", "authority_reason", "uniqueness_reason")
                if reason_fields[field]
            ]
            if asserted_reason_fields:
                return {
                    "accepted": False,
                    "error": (
                        "Unavailable target content leaves source judgments unknown; their reason fields must be empty"
                    ),
                    "invalid_fields": asserted_reason_fields,
                }
        if values["disposition"] in {"anchor", "supporting"} and (
            values["representation_status"] in {"not_target_content", "unreadable"}
            or values["relevance"] == "out_of_scope"
        ):
            return {
                "accepted": False,
                "error": (
                    "A not-target-content, unreadable, or out-of-scope source cannot be an active research input"
                ),
            }

        analysis_mode = "findings" in args
        findings: list[dict[str, Any]] = []
        finding_question_ids: list[str] = []
        finding_evidence_ids: list[str] = []
        if analysis_mode:
            findings, finding_question_ids, finding_evidence_ids, finding_error = (
                await self._validate_source_findings(
                    current=current,
                    source_set=source_set,
                    source_ref=source_ref,
                    source_id=source_id,
                    assessment_id=f"assessment:{source_id}",
                    raw_findings=args.get("findings"),
                )
            )
            if finding_error:
                return finding_error
        relation_evidence_ids = public_api._dedupe(
            [
                str(evidence_id)
                for finding in findings
                for relation in finding.get("source_relations") or []
                if isinstance(relation, dict)
                for evidence_id in relation.get("evidence_ids") or []
                if str(evidence_id)
            ]
        )
        question_ids = public_api._dedupe(
            finding_question_ids
            if analysis_mode
            else [str(item) for item in args.get("coverage_question_ids") or [] if str(item).strip()]
        )
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "").strip()
        }
        unknown_question_ids = sorted(set(question_ids) - known_question_ids)
        if unknown_question_ids:
            return {
                "accepted": False,
                "error": "coverage_question_ids must be persisted questions in the current run",
                "unknown_question_ids": unknown_question_ids,
            }
        if source_set.get("repair_targeted"):
            out_of_scope_question_ids = sorted(
                set(question_ids) - {str(item) for item in source_set.get("question_ids") or []}
            )
            if out_of_scope_question_ids:
                return {
                    "accepted": False,
                    "error": "assessment question links are outside the targeted repair scope",
                    "question_ids": out_of_scope_question_ids,
                }
        editorial_functions = public_api._dedupe(
            [str(item) for item in args.get("editorial_functions") or [] if str(item).strip()]
        )
        list_error = semantic_list_error(
            field="editorial_functions",
            values=editorial_functions,
            max_items=TRIAGE_EDITORIAL_FUNCTION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["question"],
        )
        if list_error:
            return list_error
        if values["representation_status"] in {"not_target_content", "unreadable"} and (
            question_ids or editorial_functions
        ):
            return {
                "accepted": False,
                "error": (
                    "Unavailable target content cannot establish coverage-question or editorial-function links"
                ),
                "coverage_question_ids": question_ids,
                "editorial_functions": editorial_functions,
            }
        limitations = [str(item).strip() for item in args.get("limitations") or [] if str(item).strip()]
        list_error = semantic_list_error(
            field="limitations",
            values=limitations,
            max_items=TRIAGE_LIMITATION_LIMIT,
            item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
        )
        if list_error:
            return list_error
        no_findings_reason = str(args.get("no_findings_reason") or "").strip()
        no_findings_error = semantic_field_error(
            {"no_findings_reason": no_findings_reason},
            TRIAGE_SEMANTIC_FIELD_LIMITS,
            ["no_findings_reason"],
        )
        if no_findings_error:
            return no_findings_error
        if analysis_mode and not findings and not no_findings_reason:
            return {
                "accepted": False,
                "error": "An empty findings list requires no_findings_reason after actual reading",
                "next_action": "Read the source, then explain why it established no substantive finding.",
            }
        if analysis_mode and findings and no_findings_reason:
            return {
                "accepted": False,
                "error": "no_findings_reason must be empty when substantive findings are saved",
            }
        if analysis_mode and values["representation_status"] in {"not_target_content", "unreadable"} and findings:
            return {
                "accepted": False,
                "error": "A non-target or unreadable representation cannot establish source findings",
                "next_action": "Save an empty findings list and explain the representation problem.",
            }
        evidence_ids = public_api._dedupe(
            [
                *[str(item) for item in args.get("evidence_ids") or [] if str(item).strip()],
                *finding_evidence_ids,
            ]
        )
        requires_grounded_evidence = (
            values["representation_status"] == "not_target_content"
            or (
                values["processing_status"] == "resolved"
                and values["representation_status"] in {"readable", "partial"}
            )
        )
        if requires_grounded_evidence and not evidence_ids:
            return {
                "accepted": False,
                "error": (
                    "A resolved readable/partial/not_target_content source assessment requires grounded evidence "
                    "from that source"
                ),
            }
        supports_content_judgment = (
            values["processing_status"] == "resolved"
            and values["representation_status"] in {"readable", "partial"}
        )
        selection_purpose = "source_assessment" if supports_content_judgment else "caution"
        selection_role = "triage_source_assessment" if supports_content_judgment else "caution"
        evidence_error = await self._validate_set_evidence_ids(
            source_set=source_set,
            evidence_ids=evidence_ids,
            required_source_id=source_id,
            selection_purpose=selection_purpose,
            selection_role=selection_role,
        )
        if evidence_error:
            return evidence_error
        if supports_content_judgment:
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(evidence_ids)
            if evidence_ids and len(blocked_evidence_ids) == len(evidence_ids):
                return {
                    "accepted": False,
                    "error": (
                        "Shell/error/access-page evidence cannot establish readable or partial target content; "
                        "use representation_status=not_target_content with unknown judgments and "
                        "disposition=deferred"
                    ),
                    "invalid_evidence_ids": blocked_evidence_ids,
                    "required_representation_status": "not_target_content",
                }

        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose=selection_purpose,
            role=selection_role,
            rationale="Persisted by upsert_source_assessment.",
        )
        if selection_error:
            return selection_error
        relation_selection_error = await self._select_validated_evidence_ids(
            evidence_ids=relation_evidence_ids,
            purpose="source_relationship",
            role="triage_source_relationship",
            rationale="Persisted as source-relation evidence in save_source_analysis.",
        )
        if relation_selection_error:
            return relation_selection_error

        inventory_card = next(
            (card for card in self.inventory.source_cards if str(card.source_id) == source_id),
            None,
        )
        assessment = {
            "assessment_id": f"assessment:{source_id}",
            "set_id": set_id,
            "source_ref": source_ref,
            "source_id": source_id,
            "source_version_ids": list(getattr(inventory_card, "source_version_ids", []) or []),
            "source_profile_id": getattr(inventory_card, "source_profile_id", None),
            **values,
            "representation_reason": reason_fields["representation_reason"],
            "relevance_reason": reason_fields["relevance_reason"],
            "authority_reason": reason_fields["authority_reason"],
            "uniqueness_reason": reason_fields["uniqueness_reason"],
            "disposition_reason": reason_fields["disposition_reason"],
            "coverage_question_ids": question_ids,
            "editorial_functions": editorial_functions,
            "evidence_ids": evidence_ids,
            "finding_ids": [str(item.get("finding_id") or "") for item in findings],
            "no_findings_reason": no_findings_reason,
            "limitations": limitations,
            "confidence": _confidence(args.get("confidence")),
            "updated_at": public_api._now(),
        }
        old_assessment = next(
            (
                item
                for item in current.get("source_assessments") or []
                if isinstance(item, dict) and str(item.get("source_id") or "") == source_id
            ),
            None,
        )
        old_findings = [
            item
            for item in current.get("source_findings") or []
            if isinstance(item, dict)
            and str(item.get("source_id") or "") == source_id
            and str(item.get("set_id") or "") == set_id
        ]
        analysis_changed = (
            not analysis_mode
            or _semantic_json(old_assessment) != _semantic_json(assessment)
            or _semantic_json(old_findings) != _semantic_json(findings)
        )
        if analysis_mode and analysis_changed:
            # A later source-analysis write changes the finalizer's authoritative
            # review stream. Make it read the current questions/findings again.
            current.pop("source_triage_finalizer_reads", None)
            next_revision = int(current.get("triage_semantic_revision") or 0) + 1
            for finding in findings:
                finding["semantic_revision"] = next_revision
            affected_question_ids = {
                *finding_question_ids,
                *[
                    str(link.get("question_id") or "")
                    for finding in old_findings
                    for link in finding.get("question_links") or []
                    if isinstance(link, dict)
                ],
            } - {""}
            old_representation_status = str(
                (old_assessment or {}).get("representation_status") or "unchecked"
            )
            if old_assessment is not None and old_representation_status != values["representation_status"]:
                # A representation transition can invalidate a previous deferral even when
                # neither revision has a substantive finding to link to a question.
                affected_question_ids.update(
                    str(question_id)
                    for question_id in source_set.get("question_ids") or []
                    if str(question_id)
                )
            for outcome in current.get("coverage_outcomes") or []:
                if (
                    isinstance(outcome, dict)
                    and str(outcome.get("set_id") or "") == set_id
                    and str(outcome.get("question_id") or "") in affected_question_ids
                ):
                    outcome["is_stale"] = True
            current["source_findings"] = [
                item
                for item in current.get("source_findings") or []
                if not (
                    isinstance(item, dict)
                    and str(item.get("source_id") or "") == source_id
                    and str(item.get("set_id") or "") == set_id
                )
            ]
            current["source_findings"].extend(findings)
        elif analysis_mode:
            # Preserve existing revision/timestamps on a semantic no-op.
            findings = old_findings
            assessment["finding_ids"] = [str(item.get("finding_id") or "") for item in old_findings]
        current["source_assessments"] = [
            item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_id") or "") != source_id
        ]
        current["source_assessments"].append(assessment)
        clear_pending_exact_read(source_set, source_ref=source_ref)
        self._reconcile_pending_discovered_question_attempts(
            workspace=current,
            source_set=source_set,
            reviewed_source_ref=source_ref if analysis_mode else None,
        )
        self._recompute_source_set_question_links(current, source_set)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        if analysis_changed:
            invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "source_ref": source_ref,
            "processing_status": values["processing_status"],
            "representation_status": values["representation_status"],
            "disposition": values["disposition"],
            "finding_ids": list(assessment.get("finding_ids") or []),
            "analysis_changed": analysis_changed,
            **_pending_set_work(current, source_set),
        }

    async def _blocked_shell_evidence_ids(self, evidence_ids: list[str]) -> list[str]:
        if not evidence_ids or self.evidence_toolset is None:
            return []
        blocked: list[str] = []
        for evidence_id in evidence_ids:
            evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
            if evidence is None:
                continue
            snippet = str(getattr(evidence, "snippet_text", "") or "")
            if assess_web_fetch_health(snippet, min_chars=1).state == "blocked":
                blocked.append(evidence_id)
        return blocked

    async def _validate_source_findings(
        self,
        *,
        current: dict[str, Any],
        source_set: dict[str, Any],
        source_ref: str,
        source_id: str,
        assessment_id: str,
        raw_findings: Any,
    ) -> tuple[list[dict[str, Any]], list[str], list[str], dict[str, Any] | None]:
        if not isinstance(raw_findings, list):
            return [], [], [], {
                "accepted": False,
                "error": "findings must be an array",
                "next_action": "Save every substantive source finding, or an empty array with no_findings_reason.",
            }
        questions_by_id = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        }
        ref_context = build_source_ref_context(self.inventory)
        inventory_card = next(
            (card for card in self.inventory.source_cards if str(card.source_id) == source_id),
            None,
        )
        findings: list[dict[str, Any]] = []
        all_question_ids: list[str] = []
        all_evidence_ids: list[str] = []
        seen_finding_ids: set[str] = set()
        allowed_relationships = {
            "primary_to_secondary",
            "duplicate",
            "near_duplicate",
            "contradicts",
            "updates",
            "background_for",
        }
        allowed_source_refs = {str(item) for item in source_set.get("source_refs") or []}

        for finding_index, raw_finding in enumerate(raw_findings):
            if not isinstance(raw_finding, dict):
                return [], [], [], {
                    "accepted": False,
                    "error": "Each finding must be an object",
                    "finding_index": finding_index,
                }
            statement = str(raw_finding.get("statement") or "").strip()
            if not statement:
                return [], [], [], {
                    "accepted": False,
                    "error": "Each finding requires a substantive statement",
                    "finding_index": finding_index,
                }
            field_error = semantic_field_error(
                {"finding_statement": statement},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_statement"],
            )
            if field_error:
                return [], [], [], field_error
            finding_evidence_ids = workspace_api._dedupe(
                [str(item) for item in raw_finding.get("evidence_ids") or [] if str(item).strip()]
            )
            if not finding_evidence_ids:
                return [], [], [], {
                    "accepted": False,
                    "error": "Each finding requires exact saved evidence",
                    "finding_index": finding_index,
                    "next_action": "Read an exact source window and retry with its evidence_id.",
                }
            evidence_error = await self._validate_set_evidence_ids(
                source_set=source_set,
                evidence_ids=finding_evidence_ids,
                required_source_id=source_id,
                selection_purpose="coverage_outcome",
                selection_role="triage_coverage_support",
            )
            if evidence_error:
                return [], [], [], {**evidence_error, "finding_index": finding_index}
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(finding_evidence_ids)
            if blocked_evidence_ids:
                return [], [], [], {
                    "accepted": False,
                    "error": "Shell/error/access-page evidence cannot support a substantive finding",
                    "finding_index": finding_index,
                    "invalid_evidence_ids": blocked_evidence_ids,
                }

            raw_scope = raw_finding.get("scope") or {}
            if not isinstance(raw_scope, dict):
                return [], [], [], {
                    "accepted": False,
                    "error": "finding scope must be an object",
                    "finding_index": finding_index,
                }
            scope = {
                "actor": str(raw_scope.get("actor") or "").strip(),
                "period": str(raw_scope.get("period") or "").strip(),
                "geography": str(raw_scope.get("geography") or "").strip(),
                "qualifiers": [
                    str(item).strip()
                    for item in raw_scope.get("qualifiers") or []
                    if str(item).strip()
                ],
            }
            scope_error = semantic_field_error(
                {
                    "actor": scope["actor"],
                    "period": scope["period"],
                    "geography": scope["geography"],
                },
                {key: TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"] for key in ("actor", "period", "geography")},
                ["actor", "period", "geography"],
            )
            if scope_error:
                return [], [], [], scope_error
            qualifiers_error = semantic_list_error(
                field="qualifiers",
                values=scope["qualifiers"],
                max_items=24,
                item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["finding_scope"],
            )
            if qualifiers_error:
                return [], [], [], qualifiers_error
            limitations = [
                str(item).strip()
                for item in raw_finding.get("limitations") or []
                if str(item).strip()
            ]
            limitations_error = semantic_list_error(
                field="finding_limitations",
                values=limitations,
                max_items=TRIAGE_LIMITATION_LIMIT,
                item_limit=TRIAGE_SEMANTIC_FIELD_LIMITS["assessment_limitation"],
            )
            if limitations_error:
                return [], [], [], limitations_error

            question_links: list[dict[str, Any]] = []
            seen_link_ids: set[str] = set()
            for raw_link in raw_finding.get("question_links") or []:
                if not isinstance(raw_link, dict):
                    return [], [], [], {
                        "accepted": False,
                        "error": "finding question_links items must be objects",
                        "finding_index": finding_index,
                    }
                question_id = str(raw_link.get("question_id") or "").strip()
                question = questions_by_id.get(question_id)
                if question is None:
                    return [], [], [], {
                        "accepted": False,
                        "error": "finding links must reference persisted questions",
                        "finding_index": finding_index,
                        "unknown_question_ids": [question_id],
                    }
                if question_id in seen_link_ids:
                    return [], [], [], {
                        "accepted": False,
                        "error": "A finding may contain only one link record per question",
                        "finding_index": finding_index,
                        "question_id": question_id,
                    }
                seen_link_ids.add(question_id)
                known_requirement_ids = {
                    str(item.get("requirement_id") or "")
                    for item in question.get("answer_requirements") or []
                    if isinstance(item, dict)
                }
                requirement_ids = workspace_api._dedupe(
                    [str(item) for item in raw_link.get("requirement_ids") or [] if str(item).strip()]
                )
                unknown_requirement_ids = sorted(set(requirement_ids) - known_requirement_ids)
                if unknown_requirement_ids:
                    return [], [], [], {
                        "accepted": False,
                        "error": "finding requirement links must belong to the linked question",
                        "finding_index": finding_index,
                        "question_id": question_id,
                        "unknown_requirement_ids": unknown_requirement_ids,
                    }
                if known_requirement_ids and not requirement_ids:
                    return [], [], [], {
                        "accepted": False,
                        "error": "Link the finding to the specific answer requirements it supports",
                        "finding_index": finding_index,
                        "question_id": question_id,
                        "allowed_requirement_ids": sorted(known_requirement_ids),
                    }
                rationale = str(raw_link.get("rationale") or "").strip()
                rationale_error = semantic_field_error(
                    {"finding_link_rationale": rationale},
                    TRIAGE_SEMANTIC_FIELD_LIMITS,
                    ["finding_link_rationale"],
                )
                if rationale_error:
                    return [], [], [], rationale_error
                question_links.append(
                    {
                        "question_id": question_id,
                        "requirement_ids": requirement_ids,
                        "rationale": rationale,
                    }
                )
                all_question_ids.append(question_id)

            source_relations: list[dict[str, Any]] = []
            for raw_relation in raw_finding.get("source_relations") or []:
                if not isinstance(raw_relation, dict):
                    return [], [], [], {
                        "accepted": False,
                        "error": "finding source_relations items must be objects",
                        "finding_index": finding_index,
                    }
                related_ref = str(raw_relation.get("related_source_ref") or "").strip()
                if related_ref == source_ref or related_ref not in allowed_source_refs:
                    return [], [], [], {
                        "accepted": False,
                        "error": "A source relation must name another source in the current set",
                        "finding_index": finding_index,
                        "related_source_ref": related_ref,
                    }
                related_source_id = resolve_source_ref(related_ref, ref_context)
                if not related_source_id:
                    return [], [], [], {
                        "accepted": False,
                        "error": "related_source_ref is not in the current corpus view",
                        "finding_index": finding_index,
                        "related_source_ref": related_ref,
                    }
                relationship = str(raw_relation.get("relationship") or "").strip()
                if relationship not in allowed_relationships:
                    return [], [], [], {
                        "accepted": False,
                        "error": "invalid source finding relationship",
                        "finding_index": finding_index,
                        "relationship": relationship,
                    }
                relation_rationale = str(raw_relation.get("rationale") or "").strip()
                relation_error = semantic_field_error(
                    {"finding_relation_rationale": relation_rationale},
                    TRIAGE_SEMANTIC_FIELD_LIMITS,
                    ["finding_relation_rationale"],
                )
                if relation_error:
                    return [], [], [], relation_error
                relation_evidence_ids = workspace_api._dedupe(
                    [str(item) for item in raw_relation.get("evidence_ids") or [] if str(item).strip()]
                )
                if not relation_evidence_ids:
                    return [], [], [], {
                        "accepted": False,
                        "error": "An established source relation requires exact saved evidence",
                        "finding_index": finding_index,
                        "related_source_ref": related_ref,
                    }
                relation_evidence_error = await self._validate_set_evidence_ids(
                    source_set=source_set,
                    evidence_ids=relation_evidence_ids,
                    required_source_ids={source_id, related_source_id},
                    selection_purpose="source_relationship",
                    selection_role="triage_source_relationship",
                )
                if relation_evidence_error:
                    return [], [], [], {**relation_evidence_error, "finding_index": finding_index}
                blocked_relation_evidence_ids = await self._blocked_shell_evidence_ids(
                    relation_evidence_ids
                )
                if blocked_relation_evidence_ids:
                    return [], [], [], {
                        "accepted": False,
                        "error": "Shell/error/access-page evidence cannot establish a source relation",
                        "finding_index": finding_index,
                        "invalid_evidence_ids": blocked_relation_evidence_ids,
                    }
                relation_evidence_source_ids: set[str] = set()
                for evidence_id in relation_evidence_ids:
                    evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                    if evidence is not None:
                        relation_evidence_source_ids.add(str(evidence.source_id))
                missing_evidence_source_refs = [
                    ref
                    for ref, expected_source_id in (
                        (source_ref, source_id),
                        (related_ref, related_source_id),
                    )
                    if expected_source_id not in relation_evidence_source_ids
                ]
                if missing_evidence_source_refs:
                    return [], [], [], {
                        "accepted": False,
                        "error": "A source relation requires grounded non-shell evidence from both sources",
                        "finding_index": finding_index,
                        "related_source_ref": related_ref,
                        "missing_evidence_source_refs": missing_evidence_source_refs,
                        "next_action": (
                            "Read exact material from each listed source_ref, then retry save_source_analysis "
                            "with both evidence_ids; otherwise remove the relation."
                        ),
                    }
                source_relations.append(
                    {
                        "related_source_ref": related_ref,
                        "related_source_id": related_source_id,
                        "relationship": relationship,
                        "rationale": relation_rationale,
                        "evidence_ids": relation_evidence_ids,
                    }
                )

            requested_finding_id = str(raw_finding.get("finding_id") or "").strip()
            finding_prefix = f"finding:{source_ref}:"
            if requested_finding_id.startswith("finding:") and not requested_finding_id.startswith(
                finding_prefix
            ):
                return [], [], [], {
                    "accepted": False,
                    "error": "finding_id belongs to another source",
                    "finding_id": requested_finding_id,
                    "source_ref": source_ref,
                    "next_action": "Copy this source's exact finding_id or omit it to create a stable one.",
                }
            if requested_finding_id == finding_prefix:
                return [], [], [], {
                    "accepted": False,
                    "error": "finding_id suffix is empty",
                    "next_action": "Omit finding_id to create a stable one.",
                }
            if requested_finding_id.startswith(finding_prefix):
                finding_id = requested_finding_id
            else:
                suffix = (
                    workspace_api._stable_set_id(requested_finding_id, statement, finding_index + 1)
                    if requested_finding_id
                    else hashlib.sha256(
                        json.dumps(
                            [statement, finding_evidence_ids],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()[:12]
                )
                finding_id = f"{finding_prefix}{suffix}"
            if finding_id in seen_finding_ids:
                return [], [], [], {
                    "accepted": False,
                    "error": "finding ids must be unique for this source analysis",
                    "finding_id": finding_id,
                }
            seen_finding_ids.add(finding_id)
            findings.append(
                {
                    "finding_id": finding_id,
                    "set_id": str(source_set.get("set_id") or ""),
                    "source_ref": source_ref,
                    "source_id": source_id,
                    "source_version_ids": list(getattr(inventory_card, "source_version_ids", []) or []),
                    "assessment_id": assessment_id,
                    "statement": statement,
                    "evidence_ids": finding_evidence_ids,
                    "scope": scope,
                    "limitations": limitations,
                    "question_links": question_links,
                    "source_relations": source_relations,
                    "updated_at": workspace_api._now(),
                }
            )
            all_evidence_ids.extend(finding_evidence_ids)

        return (
            findings,
            workspace_api._dedupe(all_question_ids),
            workspace_api._dedupe(all_evidence_ids),
            None,
        )

    async def update_source_finding_links_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        current = await workspace_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        finding_id = str(args.get("finding_id") or "").strip()
        finding = next(
            (
                item
                for item in current.get("source_findings") or []
                if isinstance(item, dict) and str(item.get("finding_id") or "") == finding_id
            ),
            None,
        )
        if finding is None:
            return {
                "accepted": False,
                "error": "Unknown source finding",
                "finding_id": finding_id,
                "next_action": "Page source_findings and copy an exact finding_id.",
            }
        raw_links = args.get("question_links")
        if not isinstance(raw_links, list):
            return {"accepted": False, "error": "question_links must be an array"}
        questions_by_id = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        }
        normalized_links: list[dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        for raw_link in raw_links:
            if not isinstance(raw_link, dict):
                return {"accepted": False, "error": "question_links items must be objects"}
            question_id = str(raw_link.get("question_id") or "").strip()
            question = questions_by_id.get(question_id)
            if question is None:
                return {
                    "accepted": False,
                    "error": "question_links must reference persisted questions",
                    "unknown_question_ids": [question_id],
                }
            if question_id in seen_question_ids:
                return {
                    "accepted": False,
                    "error": "Use one link record per question",
                    "question_id": question_id,
                }
            seen_question_ids.add(question_id)
            known_requirement_ids = {
                str(item.get("requirement_id") or "")
                for item in question.get("answer_requirements") or []
                if isinstance(item, dict)
            }
            requirement_ids = workspace_api._dedupe(
                [str(item) for item in raw_link.get("requirement_ids") or [] if str(item).strip()]
            )
            unknown_requirement_ids = sorted(set(requirement_ids) - known_requirement_ids)
            if unknown_requirement_ids or (known_requirement_ids and not requirement_ids):
                return {
                    "accepted": False,
                    "error": "Link the finding only to specific requirements of the referenced question",
                    "question_id": question_id,
                    "unknown_requirement_ids": unknown_requirement_ids,
                    "allowed_requirement_ids": sorted(known_requirement_ids),
                }
            rationale = str(raw_link.get("rationale") or "").strip()
            rationale_error = semantic_field_error(
                {"finding_link_rationale": rationale},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_link_rationale"],
            )
            if rationale_error:
                return rationale_error
            normalized_links.append(
                {
                    "question_id": question_id,
                    "requirement_ids": requirement_ids,
                    "rationale": rationale,
                }
            )
        old_links = [item for item in finding.get("question_links") or [] if isinstance(item, dict)]
        if _semantic_json(old_links) == _semantic_json(normalized_links):
            return {
                "accepted": True,
                "finding_id": finding_id,
                "changed": False,
                "question_ids": [str(item.get("question_id") or "") for item in normalized_links],
            }

        set_id = str(finding.get("set_id") or "")
        source_id = str(finding.get("source_id") or "")
        affected_question_ids = {
            *[str(item.get("question_id") or "") for item in old_links],
            *[str(item.get("question_id") or "") for item in normalized_links],
        } - {""}
        previous_finding = deepcopy(finding)
        next_revision = int(current.get("triage_semantic_revision") or 0) + 1
        finding["question_links"] = normalized_links
        finding["semantic_revision"] = next_revision
        finding["updated_at"] = workspace_api._now()
        source_findings = [
            item
            for item in current.get("source_findings") or []
            if isinstance(item, dict)
            and str(item.get("source_id") or "") == source_id
            and str(item.get("set_id") or "") == set_id
        ]
        source_question_ids = workspace_api._dedupe(
            [
                str(link.get("question_id") or "")
                for item in source_findings
                for link in item.get("question_links") or []
                if isinstance(link, dict) and str(link.get("question_id") or "")
            ]
        )
        for assessment in current.get("source_assessments") or []:
            if (
                isinstance(assessment, dict)
                and str(assessment.get("source_id") or "") == source_id
                and str(assessment.get("set_id") or "") == set_id
            ):
                assessment["coverage_question_ids"] = source_question_ids
                assessment["updated_at"] = workspace_api._now()
        source_set = workspace_api._workspace_set(current, set_id)
        if source_set is None:
            return {
                "accepted": False,
                "error": "Finding owner set no longer exists",
                "finding_id": finding_id,
                "set_id": set_id,
            }
        self._recompute_source_set_question_links(current, source_set)
        for outcome in current.get("coverage_outcomes") or []:
            if (
                isinstance(outcome, dict)
                and str(outcome.get("set_id") or "") == set_id
                and str(outcome.get("question_id") or "") in affected_question_ids
            ):
                outcome["is_stale"] = True
        source_set["updated_at"] = workspace_api._now()
        workspace_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        invalidate_coverage_syntheses(current)
        # The finalizer supplied the new links itself. Carry review credit only
        # when it had already paged every part of the prior finding.
        from app.generation_v2.source_triage_workspace_tools import (
            carry_finalizer_finding_review_forward,
        )

        carry_finalizer_finding_review_forward(
            current,
            previous_finding=previous_finding,
        )
        await workspace_api.save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=current,
        )
        return {
            "accepted": True,
            "finding_id": finding_id,
            "changed": True,
            "question_ids": [str(item.get("question_id") or "") for item in normalized_links],
            "stale_outcome_question_ids": sorted(affected_question_ids),
        }

    async def update_source_finding_relations_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        current = await workspace_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        finding_id = str(args.get("finding_id") or "").strip()
        finding = next(
            (
                item
                for item in current.get("source_findings") or []
                if isinstance(item, dict) and str(item.get("finding_id") or "") == finding_id
            ),
            None,
        )
        if finding is None:
            return {
                "accepted": False,
                "error": "Unknown source finding",
                "finding_id": finding_id,
                "next_action": "Page source_findings and copy an exact finding_id.",
            }
        raw_relations = args.get("source_relations")
        if not isinstance(raw_relations, list):
            return {"accepted": False, "error": "source_relations must be an array"}

        ref_context = build_source_ref_context(self.inventory)
        source_ref = str(finding.get("source_ref") or "").strip()
        source_id = str(finding.get("source_id") or "").strip()
        current_source_refs = {
            str(item)
            for item in current.get("source_order") or []
            if str(item) in ref_context.source_id_by_ref
        }
        if not current_source_refs:
            current_source_refs = set(ref_context.source_id_by_ref)
        if (
            source_ref not in current_source_refs
            or ref_context.source_id_by_ref.get(source_ref) != source_id
        ):
            return {
                "accepted": False,
                "error": "Finding owner source is not in the current run corpus view",
                "finding_id": finding_id,
                "source_ref": source_ref,
                "next_action": "Do not repair a stale finding from another run or corpus revision.",
            }
        set_id = str(finding.get("set_id") or "")
        if workspace_api._workspace_set(current, set_id) is None:
            return {
                "accepted": False,
                "error": "Finding owner set no longer exists",
                "finding_id": finding_id,
                "set_id": set_id,
            }

        allowed_relationships = {
            "primary_to_secondary",
            "duplicate",
            "near_duplicate",
            "contradicts",
            "updates",
            "background_for",
        }
        normalized_relations: list[dict[str, Any]] = []
        seen_relations: set[tuple[str, str]] = set()
        for relation_index, raw_relation in enumerate(raw_relations):
            if not isinstance(raw_relation, dict):
                return {
                    "accepted": False,
                    "error": "source_relations items must be objects",
                    "relation_index": relation_index,
                }
            related_source_ref = str(raw_relation.get("related_source_ref") or "").strip()
            related_source_id = ref_context.source_id_by_ref.get(related_source_ref)
            if related_source_ref not in current_source_refs or not related_source_id:
                return {
                    "accepted": False,
                    "error": "related_source_ref must belong to the current run corpus view",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                    "next_action": "Copy an exact related_source_ref from the current source catalog.",
                }
            if related_source_ref == source_ref:
                return {
                    "accepted": False,
                    "error": "A source finding cannot relate its source to itself",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                }
            relationship = str(raw_relation.get("relationship") or "").strip()
            if relationship not in allowed_relationships:
                return {
                    "accepted": False,
                    "error": "relationship must use a canonical enum literal",
                    "relation_index": relation_index,
                    "relationship": relationship,
                    "allowed_relationships": sorted(allowed_relationships),
                }
            relation_key = (related_source_ref, relationship)
            if relation_key in seen_relations:
                return {
                    "accepted": False,
                    "error": "Use one relation record per related source and relationship",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                    "relationship": relationship,
                }
            seen_relations.add(relation_key)
            rationale = str(raw_relation.get("rationale") or "").strip()
            rationale_error = semantic_field_error(
                {"finding_relation_rationale": rationale},
                TRIAGE_SEMANTIC_FIELD_LIMITS,
                ["finding_relation_rationale"],
            )
            if rationale_error:
                return {**rationale_error, "relation_index": relation_index}
            evidence_ids = workspace_api._dedupe(
                [str(item) for item in raw_relation.get("evidence_ids") or [] if str(item).strip()]
            )
            if not evidence_ids:
                return {
                    "accepted": False,
                    "error": "An established source relation requires exact saved evidence",
                    "relation_index": relation_index,
                    "related_source_ref": related_source_ref,
                    "next_action": "Read exact material from the related sources, or remove the relation.",
                }
            evidence_error = await self._validate_set_evidence_ids(
                source_set={"source_refs": [source_ref, related_source_ref]},
                evidence_ids=evidence_ids,
                required_source_ids={source_id, related_source_id},
                selection_purpose="source_relationship",
                selection_role="triage_source_relationship",
            )
            if evidence_error:
                relation_error = {**evidence_error, "relation_index": relation_index}
                if evidence_error.get("error") == (
                    "evidence_ids must come from the relevant source(s) in the current set"
                ):
                    relation_error["error"] = (
                        "Relation evidence_ids must belong to the finding source or related source in this run"
                    )
                    relation_error["next_action"] = (
                        "Use exact saved evidence from these two source refs, or remove the relation."
                    )
                return relation_error
            blocked_evidence_ids = await self._blocked_shell_evidence_ids(evidence_ids)
            if blocked_evidence_ids:
                return {
                    "accepted": False,
                    "error": "Shell/error/access-page evidence cannot establish a source relation",
                    "relation_index": relation_index,
                    "invalid_evidence_ids": blocked_evidence_ids,
                }
            evidence_source_ids: set[str] = set()
            for evidence_id in evidence_ids:
                evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                if evidence is not None:
                    evidence_source_ids.add(str(evidence.source_id))
            missing_evidence_source_refs = [
                ref
                for ref, expected_source_id in (
                    (source_ref, source_id),
                    (related_source_ref, related_source_id),
                )
                if expected_source_id not in evidence_source_ids
            ]
            if missing_evidence_source_refs:
                return {
                    "accepted": False,
                    "error": "A source relation requires grounded non-shell evidence from both sources",
                    "relation_index": relation_index,
                    "missing_evidence_source_refs": missing_evidence_source_refs,
                    "next_action": (
                        "Read exact material from each listed source_ref, then retry with both evidence_ids; "
                        "otherwise remove the relation."
                    ),
                }
            normalized_relations.append(
                {
                    "related_source_ref": related_source_ref,
                    "related_source_id": related_source_id,
                    "relationship": relationship,
                    "rationale": rationale,
                    "evidence_ids": evidence_ids,
                }
            )

        normalized_relations.sort(
            key=lambda item: (
                str(item.get("related_source_ref") or ""),
                str(item.get("relationship") or ""),
            )
        )
        old_relations = [
            item for item in finding.get("source_relations") or [] if isinstance(item, dict)
        ]
        if _semantic_json(old_relations) == _semantic_json(normalized_relations):
            return {
                "accepted": True,
                "finding_id": finding_id,
                "changed": False,
                "relation_count": len(normalized_relations),
            }

        relation_evidence_ids = workspace_api._dedupe(
            [
                str(evidence_id)
                for relation in normalized_relations
                for evidence_id in relation.get("evidence_ids") or []
            ]
        )
        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=relation_evidence_ids,
            purpose="source_relationship",
            role="triage_source_relationship",
            rationale="Persisted by finalizer update_source_finding_relations.",
        )
        if selection_error:
            return selection_error

        previous_finding = deepcopy(finding)
        finding["source_relations"] = normalized_relations
        finding["updated_at"] = workspace_api._now()
        self._recompute_all_set_evidence_ids(current)
        from app.generation_v2.source_triage_workspace_tools import (
            carry_finalizer_finding_review_forward,
        )

        carry_finalizer_finding_review_forward(
            current,
            previous_finding=previous_finding,
        )
        await workspace_api.save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=current,
        )
        return {
            "accepted": True,
            "finding_id": finding_id,
            "changed": True,
            "relation_count": len(normalized_relations),
            "stale_outcome_question_ids": [],
        }

    async def upsert_coverage_outcome_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
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
            return {
                "accepted": False,
                "error": "Unknown coverage question",
                "question_id": question_id,
                "next_action": "Copy an exact question_id from the persisted coverage question list.",
            }
        findings_by_id = {
            str(item.get("finding_id") or ""): item
            for item in current.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("finding_id") or "")
        }
        current_question_ids = {str(item) for item in source_set.get("question_ids") or []}
        if question_id not in current_question_ids:
            linked_current_set_findings = [
                finding
                for finding in findings_by_id.values()
                if str(finding.get("set_id") or "") == set_id
                and _finding_supports_question(finding, question_id)
            ]
            if linked_current_set_findings:
                source_set["cross_question_ids"] = public_api._dedupe(
                    [*(source_set.get("cross_question_ids") or []), question_id]
                )
                source_set["question_ids"] = public_api._dedupe(
                    [*(source_set.get("question_ids") or []), question_id]
                )
            else:
                attempted_finding_ids = _outcome_argument_finding_ids(args)
                current_set_finding_ids = [
                    finding_id
                    for finding_id in attempted_finding_ids
                    if finding_id in findings_by_id
                    and str(findings_by_id[finding_id].get("set_id") or "") == set_id
                ]
                if current_set_finding_ids:
                    self._record_pending_discovered_question_attempt(
                        source_set=source_set,
                        question_id=question_id,
                        finding_ids=current_set_finding_ids,
                        findings_by_id=findings_by_id,
                        requirement_ids=_outcome_argument_requirement_ids(args),
                    )
                    public_api._replace_workspace_set(current, source_set)
                    await public_api.save_source_triage_workspace(
                        self.db,
                        generation_run_id=self.run.id,
                        workspace=current,
                    )
                    lineage_errors = _outcome_argument_lineage_errors(
                        args=args,
                        allowed_finding_ids=set(current_set_finding_ids),
                        findings_by_id=findings_by_id,
                        set_id=set_id,
                        question_id=question_id,
                    )
                    return {
                        "accepted": False,
                        "error": (
                            "A discovered question becomes set work only through a persisted finding link"
                        ),
                        "question_id": question_id,
                        "lineage_errors": lineage_errors,
                        "next_action": (
                            "Resave each listed finding's owner source with a question_link to this question and "
                            "its exact requirement_ids, then retry the outcome. If the attempted link was wrong, "
                            "resave every listed owner source with its complete reviewed findings; an identical "
                            "accepted analysis is valid confirmation that no such link exists."
                        ),
                    }
                return {
                    "accepted": False,
                    "error": "question_id must belong to the current source set or have a linked current-set finding",
                    "question_id": question_id,
                    "next_action": (
                        "Use an assigned question, or first persist a source finding linked to this discovered question."
                    ),
                }
        if source_set.get("repair_targeted") and question_id not in {
            str(item) for item in source_set.get("repair_question_ids") or []
        }:
            return {
                "accepted": False,
                "error": "coverage outcome is outside the targeted repair scope",
                "question_id": question_id,
                "allowed_question_ids": list(source_set.get("repair_question_ids") or []),
            }
        status = str(args.get("status") or "unknown").strip()
        if status not in {"covered", "partial", "missing", "conflicted", "deferred"}:
            return {"accepted": False, "error": "coverage outcome status is not final", "status": status}
        requested_next_action = str(args.get("next_action") or "research").strip()
        if requested_next_action != "research":
            return {
                "accepted": False,
                "error": "per_set_outcome_cannot_route_run",
                "message": (
                    "A source-set outcome is local. Only the run-wide coverage synthesis may request "
                    "source acquisition, omission, or completion."
                ),
            }
        next_action = "research"

        finding_lineage_mode = "finding_ids" in args or "requirement_results" in args
        requirement_results: list[dict[str, Any]] = []
        finding_ids: list[str] = []
        if finding_lineage_mode:
            expected_requirement_ids = _local_requirement_ids(
                question=question or {"question_id": question_id, "answer_requirements": []},
                source_set=source_set,
                findings_by_id=findings_by_id,
                question_id=question_id,
            )
            requirement_results, result_finding_ids, requirement_error = self._validate_requirement_results(
                question=question or {"question_id": question_id, "answer_requirements": []},
                raw_results=args.get("requirement_results"),
                overall_status=status,
                findings_by_id=findings_by_id,
                set_id=set_id,
                allow_global_next_action=False,
                expected_requirement_ids=expected_requirement_ids,
            )
            if requirement_error:
                return requirement_error
            provided_finding_ids = public_api._dedupe(
                [str(item) for item in args.get("finding_ids") or [] if str(item).strip()]
            )
            if (
                (question or {}).get("answer_requirements")
                and "finding_ids" in args
                and set(provided_finding_ids) != set(result_finding_ids)
            ):
                return {
                    "accepted": False,
                    "error": (
                        "For a structured question, outcome finding_ids must exactly equal the findings used "
                        "by its requirement results"
                    ),
                    "provided_finding_ids": provided_finding_ids,
                    "required_finding_ids": result_finding_ids,
                    "next_action": (
                        "Remove the legacy top-level finding_ids field, or make it match the union of "
                        "requirement_results[].finding_ids."
                    ),
                }
            finding_ids = public_api._dedupe(
                result_finding_ids
                if (question or {}).get("answer_requirements")
                else [*provided_finding_ids, *result_finding_ids]
            )
            unknown_finding_ids = sorted(set(finding_ids) - set(findings_by_id))
            wrong_set_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and str(findings_by_id[finding_id].get("set_id") or "") != set_id
            )
            unlinked_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and not _finding_supports_question(findings_by_id[finding_id], question_id)
            )
            if unknown_finding_ids or wrong_set_finding_ids or unlinked_finding_ids:
                lineage_errors = _finding_lineage_errors(
                    finding_ids=finding_ids,
                    findings_by_id=findings_by_id,
                    set_id=set_id,
                    question_id=question_id,
                )
                return {
                    "accepted": False,
                    "error": "Outcome may use only current-set findings explicitly linked to this question",
                    "question_id": question_id,
                    "unknown_finding_ids": unknown_finding_ids,
                    "wrong_set_finding_ids": wrong_set_finding_ids,
                    "unlinked_finding_ids": unlinked_finding_ids,
                    "lineage_errors": lineage_errors,
                    "next_action": _lineage_repair_next_action(lineage_errors),
                }
            selected_findings = [findings_by_id[finding_id] for finding_id in finding_ids]
            source_refs = public_api._dedupe(
                [str(item.get("source_ref") or "") for item in selected_findings]
            )
            evidence_ids = public_api._dedupe(
                [
                    str(evidence_id)
                    for item in selected_findings
                    for evidence_id in item.get("evidence_ids") or []
                ]
            )
        else:
            # Compatibility for completed V2 work written before findings.
            source_refs = public_api._dedupe(
                [str(item) for item in args.get("source_refs") or [] if str(item).strip()]
            )
            evidence_ids = public_api._dedupe(
                [str(item) for item in args.get("evidence_ids") or [] if str(item).strip()]
            )
        invalid_refs = sorted(set(source_refs) - set(source_set.get("source_refs") or []))
        if invalid_refs:
            return {
                "accepted": False,
                "error": "coverage outcome source_refs must belong to the current source set",
                "invalid_source_refs": invalid_refs,
            }
        ref_context = build_source_ref_context(self.inventory)
        unresolved_refs = [ref for ref in source_refs if not resolve_source_ref(ref, ref_context)]
        if unresolved_refs:
            return {
                "accepted": False,
                "error": "coverage outcome source_refs could not be resolved in the current corpus view",
                "invalid_source_refs": unresolved_refs,
            }
        source_ids = [resolve_source_ref(ref, ref_context) for ref in source_refs]
        source_ids = [source_id for source_id in source_ids if source_id]
        selection_purpose, selection_role = {
            "covered": ("coverage_outcome", "triage_coverage_support"),
            "partial": ("coverage_outcome", "triage_coverage_support"),
            "conflicted": ("contradiction", "contradiction"),
            "missing": ("gap", "gap"),
            "deferred": ("gap", "gap"),
        }[status]
        evidence_error = await self._validate_set_evidence_ids(
            source_set=source_set,
            evidence_ids=evidence_ids,
            required_source_ids=set(source_ids),
            selection_purpose=selection_purpose,
            selection_role=selection_role,
        )
        if evidence_error:
            return evidence_error

        semantic_fields = {
            "coverage_summary": str(args.get("orientation_summary") or "").strip(),
            "missing_detail": str(args.get("missing_detail") or "").strip(),
            "conflict_summary": str(args.get("conflict_summary") or "").strip(),
            "deferred_reason": str(args.get("deferred_reason") or "").strip(),
        }
        field_error = semantic_field_error(
            semantic_fields,
            {
                "coverage_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_summary"],
                "missing_detail": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                "conflict_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                "deferred_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
            },
            list(semantic_fields),
        )
        if field_error:
            return field_error
        assessments_by_ref = {
            str(item.get("source_ref") or ""): item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        }
        has_representation_blocker = any(
            str((assessments_by_ref.get(str(source_ref)) or {}).get("representation_status") or "")
            in {"partial", "not_target_content", "unreadable"}
            for source_ref in source_set.get("source_refs") or []
        )
        deferred_requirement_ids = [
            str(result.get("requirement_id") or "")
            for result in requirement_results
            if str(result.get("status") or "") == "deferred"
        ]
        if deferred_requirement_ids and not has_representation_blocker:
            return {
                "accepted": False,
                "error": "A deferred requirement needs a checked representation blocker in this source set",
                "requirement_ids": deferred_requirement_ids,
                "next_action": (
                    "Continue reading and save the source analysis; if checked readable material is absent, "
                    "record the requirement as missing instead."
                ),
            }
        if status in {"covered", "partial", "conflicted"} and not source_ids:
            return {"accepted": False, "error": f"{status} outcome requires at least one source_ref"}
        if status in {"covered", "partial", "conflicted"} and not evidence_ids:
            return {"accepted": False, "error": f"{status} outcome requires saved evidence"}
        if status == "covered" and not semantic_fields["coverage_summary"]:
            return {"accepted": False, "error": "covered outcome requires orientation_summary"}
        if status == "partial" and not (
            semantic_fields["coverage_summary"] and semantic_fields["missing_detail"]
        ):
            return {
                "accepted": False,
                "error": "partial outcome requires both orientation_summary and a concrete missing_detail",
            }
        if status == "missing" and not semantic_fields["missing_detail"]:
            return {"accepted": False, "error": "missing outcome requires missing_detail"}
        if status == "conflicted" and not semantic_fields["conflict_summary"]:
            return {"accepted": False, "error": "conflicted outcome requires conflict_summary"}
        if status == "deferred" and not semantic_fields["deferred_reason"]:
            return {"accepted": False, "error": "deferred outcome requires deferred_reason"}
        if status == "deferred":
            if not has_representation_blocker:
                return {
                    "accepted": False,
                    "error": (
                        "deferred outcome requires a checked partial/not_target_content/unreadable representation; "
                        "continue reading unfinished sources or use missing for checked absence"
                    ),
                    "next_action": (
                        "Save the remaining source analysis, then retry with deferred only for a real "
                        "representation blocker or missing after checked absence."
                    ),
                }
        contradictory_fields = {
            "covered": [semantic_fields["missing_detail"], semantic_fields["deferred_reason"]],
            "partial": [semantic_fields["deferred_reason"]],
            "missing": [
                semantic_fields["coverage_summary"],
                semantic_fields["conflict_summary"],
                semantic_fields["deferred_reason"],
            ],
            "conflicted": [semantic_fields["deferred_reason"]],
            "deferred": [
                semantic_fields["coverage_summary"],
                semantic_fields["missing_detail"],
                semantic_fields["conflict_summary"],
            ],
        }[status]
        if any(contradictory_fields):
            return {
                "accepted": False,
                "error": "coverage outcome contains fields that contradict its final status",
                "status": status,
            }

        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose=selection_purpose,
            role=selection_role,
            rationale="Persisted by upsert_coverage_outcome.",
        )
        if selection_error:
            return selection_error

        outcome_id = f"outcome:{question_id}:{set_id}"
        outcome = {
            "outcome_id": outcome_id,
            "set_id": set_id,
            "question_id": question_id,
            "status": status,
            "source_refs": source_refs,
            "source_ids": source_ids,
            "evidence_ids": evidence_ids,
            "finding_ids": finding_ids,
            "requirement_results": requirement_results,
            "orientation_summary": semantic_fields["coverage_summary"],
            "missing_detail": semantic_fields["missing_detail"],
            "conflict_summary": semantic_fields["conflict_summary"],
            "deferred_reason": semantic_fields["deferred_reason"],
            "confidence": _confidence(args.get("confidence")),
            "next_action": next_action,
            "semantic_revision": int(current.get("triage_semantic_revision") or 0) + 1,
            "is_stale": False,
            "updated_at": public_api._now(),
        }
        current["coverage_outcomes"] = [
            item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and str(item.get("outcome_id") or "") != outcome_id
        ]
        current["coverage_outcomes"].append(outcome)
        source_set["pending_discovered_question_attempts"] = [
            item
            for item in source_set.get("pending_discovered_question_attempts") or []
            if not isinstance(item, dict) or str(item.get("question_id") or "") != question_id
        ]
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        self._recompute_all_set_evidence_ids(current)
        invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "outcome_id": outcome_id,
            "question_id": question_id,
            "status": status,
            **_pending_set_work(current, source_set),
        }

    @staticmethod
    def _validate_requirement_results(
        *,
        question: dict[str, Any],
        raw_results: Any,
        overall_status: str,
        findings_by_id: dict[str, dict[str, Any]],
        set_id: str | None,
        allow_global_next_action: bool,
        expected_requirement_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
        requirements = [
            item for item in question.get("answer_requirements") or [] if isinstance(item, dict)
        ]
        known_ids = {
            str(item.get("requirement_id") or "") for item in requirements if str(item.get("requirement_id") or "")
        }
        expected_ids = known_ids if expected_requirement_ids is None else set(expected_requirement_ids)
        invalid_expected_ids = sorted(expected_ids - known_ids)
        if invalid_expected_ids:
            return [], [], {
                "accepted": False,
                "error": "Source-set requirement assignment references unknown question requirements",
                "unknown_requirement_ids": invalid_expected_ids,
            }
        if raw_results is None:
            raw_results = []
        if not isinstance(raw_results, list):
            return [], [], {"accepted": False, "error": "requirement_results must be an array"}
        if not expected_ids:
            if raw_results:
                return [], [], {
                    "accepted": False,
                    "error": "Legacy question has no persisted answer requirements",
                    "next_action": "Use an empty requirement_results array for this legacy question.",
                }
            return [], [], None
        supplied_ids = [
            str(item.get("requirement_id") or "")
            for item in raw_results
            if isinstance(item, dict)
        ]
        missing_ids = sorted(expected_ids - set(supplied_ids))
        unknown_ids = sorted(set(supplied_ids) - expected_ids)
        duplicate_ids = sorted(
            requirement_id for requirement_id in set(supplied_ids) if supplied_ids.count(requirement_id) > 1
        )
        if len(supplied_ids) != len(raw_results) or missing_ids or unknown_ids or duplicate_ids:
            return [], [], {
                "accepted": False,
                "error": "Provide exactly one result for every requirement assigned to or found by this set",
                "missing_requirement_ids": missing_ids,
                "unknown_requirement_ids": unknown_ids,
                "duplicate_requirement_ids": duplicate_ids,
            }

        normalized: list[dict[str, Any]] = []
        all_finding_ids: list[str] = []
        result_statuses: list[str] = []
        for raw_result in raw_results:
            requirement_id = str(raw_result.get("requirement_id") or "")
            status = str(raw_result.get("status") or "").strip()
            if status not in {"covered", "partial", "missing", "conflicted", "deferred"}:
                return [], [], {
                    "accepted": False,
                    "error": "requirement result status is not final",
                    "requirement_id": requirement_id,
                    "status": status,
                }
            finding_ids = workspace_api._dedupe(
                [str(item) for item in raw_result.get("finding_ids") or [] if str(item).strip()]
            )
            unknown_finding_ids = sorted(set(finding_ids) - set(findings_by_id))
            wrong_set_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and set_id is not None
                and str(findings_by_id[finding_id].get("set_id") or "") != set_id
            )
            unlinked_finding_ids = sorted(
                finding_id
                for finding_id in finding_ids
                if finding_id in findings_by_id
                and not _finding_supports_requirement(
                    findings_by_id[finding_id],
                    str(question.get("question_id") or ""),
                    requirement_id,
                )
            )
            if unknown_finding_ids or wrong_set_finding_ids or unlinked_finding_ids:
                lineage_errors = _finding_lineage_errors(
                    finding_ids=finding_ids,
                    findings_by_id=findings_by_id,
                    set_id=set_id,
                    question_id=str(question.get("question_id") or ""),
                    requirement_id=requirement_id,
                )
                return [], [], {
                    "accepted": False,
                    "error": "Requirement result may use only findings linked to this question requirement",
                    "question_id": str(question.get("question_id") or ""),
                    "requirement_id": requirement_id,
                    "unknown_finding_ids": unknown_finding_ids,
                    "wrong_set_finding_ids": wrong_set_finding_ids,
                    "unlinked_finding_ids": unlinked_finding_ids,
                    "lineage_errors": lineage_errors,
                    "next_action": _lineage_repair_next_action(lineage_errors),
                }
            summary = str(raw_result.get("summary") or "").strip()
            missing_detail = str(raw_result.get("missing_detail") or "").strip()
            conflict_summary = str(raw_result.get("conflict_summary") or "").strip()
            deferred_reason = str(raw_result.get("deferred_reason") or "").strip()
            field_error = semantic_field_error(
                {
                    "requirement_summary": summary,
                    "missing_detail": missing_detail,
                    "conflict_summary": conflict_summary,
                    "deferred_reason": deferred_reason,
                },
                {
                    "requirement_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["requirement_summary"],
                    "missing_detail": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "conflict_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_detail"],
                    "deferred_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
                },
                ["requirement_summary", "missing_detail", "conflict_summary", "deferred_reason"],
            )
            if field_error:
                return [], [], field_error
            if status in {"covered", "partial", "conflicted"} and not finding_ids:
                return [], [], {
                    "accepted": False,
                    "error": f"{status} requirement result requires a linked finding",
                    "requirement_id": requirement_id,
                }
            if status == "covered" and (not summary or missing_detail or conflict_summary or deferred_reason):
                return [], [], {
                    "accepted": False,
                    "error": "covered requirement result needs summary and no gap/conflict/deferred fields",
                    "requirement_id": requirement_id,
                }
            if status == "partial" and (not summary or not missing_detail or conflict_summary or deferred_reason):
                return [], [], {
                    "accepted": False,
                    "error": "partial requirement result needs summary plus a concrete missing_detail",
                    "requirement_id": requirement_id,
                }
            if status == "missing" and (
                finding_ids or summary or not missing_detail or conflict_summary or deferred_reason
            ):
                return [], [], {
                    "accepted": False,
                    "error": "missing requirement result needs only a concrete missing_detail",
                    "requirement_id": requirement_id,
                }
            if status == "conflicted" and (not conflict_summary or deferred_reason):
                return [], [], {
                    "accepted": False,
                    "error": "conflicted requirement result needs linked findings and conflict_summary",
                    "requirement_id": requirement_id,
                }
            if status == "deferred" and (
                finding_ids or summary or missing_detail or conflict_summary or not deferred_reason
            ):
                return [], [], {
                    "accepted": False,
                    "error": "deferred requirement result needs only a representation-blocked reason",
                    "requirement_id": requirement_id,
                }
            next_action = str(raw_result.get("next_action") or "research").strip()
            if allow_global_next_action:
                if next_action not in {"research", "source_acquisition", "omit", "none"}:
                    return [], [], {
                        "accepted": False,
                        "error": "invalid requirement result next_action",
                        "requirement_id": requirement_id,
                    }
            elif next_action != "research":
                return [], [], {
                    "accepted": False,
                    "error": "A local requirement result cannot route run-wide work",
                    "requirement_id": requirement_id,
                }
            normalized.append(
                {
                    "requirement_id": requirement_id,
                    "status": status,
                    "finding_ids": finding_ids,
                    "summary": summary,
                    "missing_detail": missing_detail,
                    "conflict_summary": conflict_summary,
                    "deferred_reason": deferred_reason,
                    "confidence": _confidence(raw_result.get("confidence")),
                    "next_action": next_action,
                }
            )
            all_finding_ids.extend(finding_ids)
            result_statuses.append(status)

        consistency_error = _requirement_status_consistency_error(
            overall_status=overall_status,
            requirement_statuses=result_statuses,
        )
        if consistency_error:
            return [], [], consistency_error
        return normalized, workspace_api._dedupe(all_finding_ids), None

    async def upsert_coverage_synthesis_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Persist one question-level synthesis across all completed source sets."""

        from app.generation_v2.source_triage_canonical import (
            coverage_question_material_lineage,
            coverage_synthesis_input_hash,
            research_eligible,
        )

        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        question_id = str(args.get("question_id") or "").strip()
        known_question_ids = {
            str(item.get("question_id") or "")
            for item in current.get("coverage_questions") or []
            if isinstance(item, dict)
        }
        if question_id not in known_question_ids:
            return {"accepted": False, "error": "Unknown coverage question", "question_id": question_id}

        status = str(args.get("status") or "").strip()
        if status not in {"covered", "partial", "missing", "conflicted", "deferred"}:
            return {"accepted": False, "error": "coverage synthesis status is not final", "status": status}
        question = next(
            (
                item
                for item in current.get("coverage_questions") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            {"question_id": question_id, "answer_requirements": []},
        )
        findings_by_id = {
            str(item.get("finding_id") or ""): item
            for item in current.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("finding_id") or "")
        }
        requirement_results, finding_ids, requirement_error = self._validate_requirement_results(
            question=question,
            raw_results=args.get("requirement_results"),
            overall_status=status,
            findings_by_id=findings_by_id,
            set_id=None,
            allow_global_next_action=True,
            expected_requirement_ids=None,
        )
        if requirement_error:
            return requirement_error
        question_outcomes = [
            item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
        ]
        stale_outcome_refs = sorted(
            str(item.get("outcome_id") or "")
            for item in question_outcomes
            if bool(item.get("is_stale"))
        )
        if stale_outcome_refs:
            return {
                "accepted": False,
                "error": "Run-wide synthesis cannot use stale local outcomes",
                "stale_outcome_refs": stale_outcome_refs,
                "next_action": "Update the affected local outcomes from current findings first.",
            }
        outcome_statuses = {str(item.get("status") or "") for item in question_outcomes}
        if status == "missing" and outcome_statuses != {"missing"}:
            return {
                "accepted": False,
                "error": "missing_synthesis_requires_all_set_outcomes_missing",
                "outcome_statuses": sorted(outcome_statuses),
            }
        if status == "deferred" and (
            "deferred" not in outcome_statuses
            or not outcome_statuses.issubset({"missing", "deferred"})
        ):
            return {
                "accepted": False,
                "error": "deferred_synthesis_cannot_discard_active_set_outcomes",
                "outcome_statuses": sorted(outcome_statuses),
            }
        requested_next_action = str(args.get("next_action") or "research").strip()
        if requested_next_action not in {"research", "source_acquisition", "omit", "none"}:
            return {"accepted": False, "error": "invalid next_action"}
        next_action = _question_next_action_from_requirements(
            requirement_results=requirement_results,
            fallback=requested_next_action,
        )
        if next_action == "source_acquisition":
            if status == "covered":
                return {
                    "accepted": False,
                    "error": "covered_synthesis_cannot_request_source_acquisition",
                }
            assigned_sets = [
                item
                for item in current.get("source_sets") or []
                if isinstance(item, dict)
                and question_id in {str(value) for value in item.get("question_ids") or []}
            ]
            assigned_set_ids = {str(item.get("set_id") or "") for item in assigned_sets}
            outcome_set_ids = {
                str(item.get("set_id") or "")
                for item in current.get("coverage_outcomes") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            }
            unfinished_set_ids = sorted(
                str(item.get("set_id") or "")
                for item in assigned_sets
                if str(item.get("status") or "") not in {"mapped", "deferred"}
            )
            missing_outcome_set_ids = sorted(assigned_set_ids - outcome_set_ids)
            if not assigned_set_ids or unfinished_set_ids or missing_outcome_set_ids:
                return {
                    "accepted": False,
                    "error": "source_acquisition_requires_all_assigned_set_outcomes",
                    "unfinished_set_ids": unfinished_set_ids,
                    "missing_outcome_set_ids": missing_outcome_set_ids,
                }

        active_outcomes = [
            item
            for item in question_outcomes
            if str(item.get("status") or "") in {"covered", "partial", "conflicted"}
        ]
        source_ref_by_id = {
            str(item.get("source_id") or ""): str(item.get("source_ref") or "")
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and item.get("source_id") and item.get("source_ref")
        }
        derived_source_refs, derived_evidence_ids, outcome_refs = coverage_synthesis_lineage(
            question_outcomes,
            status=status,
            source_ref_by_id=source_ref_by_id,
        )
        if finding_ids:
            selected_findings = [findings_by_id[finding_id] for finding_id in finding_ids]
            derived_source_refs = public_api._dedupe(
                [str(item.get("source_ref") or "") for item in selected_findings]
            )
            derived_evidence_ids = public_api._dedupe(
                [
                    str(evidence_id)
                    for item in selected_findings
                    for evidence_id in item.get("evidence_ids") or []
                ]
            )
        manual_lineage = "source_refs" in args or "evidence_ids" in args
        source_refs = public_api._dedupe(
            [
                str(item)
                for item in (
                    args.get("source_refs") if manual_lineage else derived_source_refs
                )
                or []
                if str(item).strip()
            ]
        )
        unknown_source_refs = sorted(set(source_refs) - {str(item) for item in current.get("source_order") or []})
        if unknown_source_refs:
            return {
                "accepted": False,
                "error": "coverage synthesis source_refs must belong to the current corpus view",
                "invalid_source_refs": unknown_source_refs,
            }
        ref_context = build_source_ref_context(self.inventory)
        source_ids = [resolve_source_ref(ref, ref_context) for ref in source_refs]
        unresolved_refs = [ref for ref, source_id in zip(source_refs, source_ids, strict=True) if not source_id]
        if unresolved_refs:
            return {
                "accepted": False,
                "error": "coverage synthesis source_refs could not be resolved in the current corpus view",
                "invalid_source_refs": unresolved_refs,
            }
        source_ids = [source_id for source_id in source_ids if source_id]

        material_lineage = coverage_question_material_lineage(current, question_id)
        active_outcome_source_refs = {
            *[
                str(source_ref)
                for outcome in active_outcomes
                for source_ref in outcome.get("source_refs") or []
            ],
            *[
                source_ref_by_id.get(str(source_id), "")
                for outcome in active_outcomes
                for source_id in outcome.get("source_ids") or []
            ],
        } - {""}
        allowed_source_refs = (
            active_outcome_source_refs
            if status in {"covered", "partial", "conflicted"}
            else set(material_lineage["source_refs"])
        )
        unlinked_source_refs = sorted(set(source_refs) - allowed_source_refs)
        if unlinked_source_refs:
            return {
                "accepted": False,
                "error": "coverage synthesis sources must be attached to this question by an assessment, source set, or per-set outcome",
                "invalid_source_refs": unlinked_source_refs,
            }

        assessments_by_ref = {
            str(item.get("source_ref") or ""): item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
        }
        if status in {"covered", "partial", "conflicted"}:
            ineligible_refs = sorted(
                ref for ref in source_refs if ref not in assessments_by_ref or not research_eligible(assessments_by_ref[ref])
            )
            if ineligible_refs:
                return {
                    "accepted": False,
                    "error": "Active coverage synthesis cannot use deferred, unreadable, out-of-scope, or unusable sources",
                    "invalid_source_refs": ineligible_refs,
                }

        evidence_ids = public_api._dedupe(
            [
                str(item)
                for item in (
                    args.get("evidence_ids") if manual_lineage else derived_evidence_ids
                )
                or []
                if str(item).strip()
            ]
        )
        active_outcome_evidence_ids = {
            str(evidence_id)
            for outcome in active_outcomes
            for evidence_id in outcome.get("evidence_ids") or []
            if str(evidence_id).strip()
        }
        allowed_evidence_ids = (
            active_outcome_evidence_ids
            if status in {"covered", "partial", "conflicted"}
            else set(material_lineage["evidence_ids"])
        )
        unreviewed_evidence_ids = sorted(
            set(evidence_ids) - allowed_evidence_ids
        )
        if unreviewed_evidence_ids:
            return {
                "accepted": False,
                "error": "coverage synthesis may use only evidence attached to this question by an assessment or per-set outcome",
                "invalid_evidence_ids": unreviewed_evidence_ids,
            }

        if status in {"covered", "partial", "conflicted"}:
            if self.evidence_toolset is None:
                return {
                    "accepted": False,
                    "error": "Evidence validation is unavailable; evidence-backed triage state was not persisted",
                }
            evidence_source_refs: set[str] = set()
            for evidence_id in evidence_ids:
                evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
                if evidence is None:
                    continue
                evidence_source_ref = await self.evidence_toolset._source_ref_for_id(evidence.source_id)
                if evidence_source_ref:
                    evidence_source_refs.add(str(evidence_source_ref))
            unbacked_source_refs = sorted(set(source_refs) - evidence_source_refs)
            if unbacked_source_refs:
                return {
                    "accepted": False,
                    "error": "coverage_synthesis_source_refs_require_selected_evidence",
                    "invalid_source_refs": unbacked_source_refs,
                }

        semantic_fields = {
            "coverage_summary": str(args.get("orientation_summary") or "").strip(),
            "missing_detail": str(args.get("missing_detail") or "").strip(),
            "conflict_summary": str(args.get("conflict_summary") or "").strip(),
            "deferred_reason": str(args.get("deferred_reason") or "").strip(),
        }
        field_error = semantic_field_error(
            semantic_fields,
            {
                "coverage_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_summary"],
                "missing_detail": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                "conflict_summary": TRIAGE_SEMANTIC_FIELD_LIMITS["coverage_synthesis_detail"],
                "deferred_reason": TRIAGE_SEMANTIC_FIELD_LIMITS["deferred_reason"],
            },
            list(semantic_fields),
        )
        if field_error:
            return field_error

        if status in {"covered", "partial", "conflicted"} and not source_ids:
            return {"accepted": False, "error": f"{status} synthesis requires at least one source_ref"}
        if status in {"covered", "partial", "conflicted"} and not evidence_ids:
            return {"accepted": False, "error": f"{status} synthesis requires saved evidence"}
        if status == "covered" and not semantic_fields["coverage_summary"]:
            return {"accepted": False, "error": "covered synthesis requires orientation_summary"}
        if status == "partial" and not (
            semantic_fields["coverage_summary"] and semantic_fields["missing_detail"]
        ):
            return {
                "accepted": False,
                "error": "partial synthesis requires both orientation_summary and a still-open missing_detail",
            }
        if status == "missing" and not semantic_fields["missing_detail"]:
            return {"accepted": False, "error": "missing synthesis requires missing_detail"}
        if status == "conflicted" and not semantic_fields["conflict_summary"]:
            return {"accepted": False, "error": "conflicted synthesis requires conflict_summary"}
        if status == "deferred" and not semantic_fields["deferred_reason"]:
            return {"accepted": False, "error": "deferred synthesis requires deferred_reason"}
        if status == "deferred" and not any(
            isinstance(item, dict)
            and str(item.get("question_id") or "") == question_id
            and str(item.get("status") or "") == "deferred"
            for item in current.get("coverage_outcomes") or []
        ):
            return {
                "accepted": False,
                "error": "deferred synthesis requires at least one representation-blocked per-set outcome",
            }
        contradictory_fields = {
            "covered": [semantic_fields["missing_detail"], semantic_fields["deferred_reason"]],
            "partial": [semantic_fields["deferred_reason"]],
            "missing": [
                semantic_fields["coverage_summary"],
                semantic_fields["conflict_summary"],
                semantic_fields["deferred_reason"],
            ],
            "conflicted": [semantic_fields["deferred_reason"]],
            "deferred": [
                semantic_fields["coverage_summary"],
                semantic_fields["missing_detail"],
                semantic_fields["conflict_summary"],
            ],
        }[status]
        if any(contradictory_fields):
            return {
                "accepted": False,
                "error": "coverage synthesis contains fields that contradict its final status",
                "status": status,
            }

        selection_purpose, selection_role = {
            "covered": ("coverage_outcome", "triage_coverage_synthesis"),
            "partial": ("coverage_outcome", "triage_coverage_synthesis"),
            "conflicted": ("contradiction", "triage_coverage_synthesis"),
            "missing": ("gap", "gap"),
            "deferred": ("gap", "gap"),
        }[status]
        evidence_error = await self._validate_set_evidence_ids(
            source_set={"source_refs": list(current.get("source_order") or [])},
            evidence_ids=evidence_ids,
            required_source_ids=set(source_ids),
            selection_purpose=selection_purpose,
            selection_role=selection_role,
        )
        if evidence_error:
            return evidence_error
        selection_error = await self._select_validated_evidence_ids(
            evidence_ids=evidence_ids,
            purpose=selection_purpose,
            role=selection_role,
            rationale="Persisted by upsert_coverage_synthesis.",
        )
        if selection_error:
            return selection_error

        synthesis_id = f"synthesis:{question_id}"
        synthesis = {
            "outcome_id": synthesis_id,
            "question_id": question_id,
            "status": status,
            "source_refs": source_refs,
            "source_ids": source_ids,
            "evidence_ids": evidence_ids,
            "outcome_refs": outcome_refs,
            "finding_ids": finding_ids,
            "requirement_results": requirement_results,
            "orientation_summary": semantic_fields["coverage_summary"],
            "missing_detail": semantic_fields["missing_detail"],
            "conflict_summary": semantic_fields["conflict_summary"],
            "deferred_reason": semantic_fields["deferred_reason"],
            "confidence": _confidence(args.get("confidence")),
            "next_action": next_action,
            "input_revision": int(current.get("triage_semantic_revision") or 0),
            "input_hash": coverage_synthesis_input_hash(current, question_id),
            "updated_at": public_api._now(),
        }
        current["coverage_syntheses"] = [
            item
            for item in current.get("coverage_syntheses") or []
            if not isinstance(item, dict) or str(item.get("question_id") or "") != question_id
        ]
        current["coverage_syntheses"].append(synthesis)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {
            "accepted": True,
            "synthesis_ref": synthesis_id,
            "question_id": question_id,
            "status": status,
            "lineage_derived_from_outcomes": not manual_lineage,
            "source_ref_count": len(source_refs),
            "evidence_count": len(evidence_ids),
        }

    async def _validate_set_evidence_ids(
        self,
        *,
        source_set: dict[str, Any],
        evidence_ids: list[str],
        required_source_id: str | None = None,
        required_source_ids: set[str] | None = None,
        selection_purpose: str,
        selection_role: str,
    ) -> dict[str, Any] | None:
        if not evidence_ids:
            return None
        if self.evidence_toolset is None:
            return {
                "accepted": False,
                "error": "Evidence validation is unavailable; evidence-backed triage state was not persisted",
                "invalid_evidence_ids": evidence_ids,
            }
        invalid: list[str] = []
        ungrounded: list[str] = []
        not_exact_read: list[str] = []
        allowed_refs = set(source_set.get("source_refs") or [])
        exact_read_checker = getattr(
            self.evidence_toolset,
            "_evidence_has_current_exact_read",
            None,
        )
        for evidence_id in evidence_ids:
            evidence = await self.evidence_toolset._evidence_in_run(evidence_id)
            if evidence is None:
                invalid.append(evidence_id)
                continue
            evidence_ref = await self.evidence_toolset._source_ref_for_id(evidence.source_id)
            if (
                evidence_ref not in allowed_refs
                or (required_source_id is not None and str(evidence.source_id) != required_source_id)
                or (required_source_ids is not None and str(evidence.source_id) not in required_source_ids)
            ):
                invalid.append(evidence_id)
                continue
            if not str(getattr(evidence, "snippet_text", "") or "").strip() or (
                _unsupported_model_read_selected_as_support(
                    evidence,
                    purpose=selection_purpose,
                    role=selection_role,
                )
            ):
                ungrounded.append(evidence_id)
                continue
            if callable(exact_read_checker) and not exact_read_checker(evidence):
                not_exact_read.append(evidence_id)
        if invalid:
            return {
                "accepted": False,
                "error": "evidence_ids must come from the relevant source(s) in the current set",
                "invalid_evidence_ids": invalid,
            }
        if ungrounded:
            return {
                "accepted": False,
                "error": "evidence_ids must contain grounded source text that supports the persisted triage status",
                "invalid_evidence_ids": ungrounded,
            }
        if not_exact_read:
            return {
                "accepted": False,
                "error": "Semantic triage evidence must have a current substantive exact read",
                "not_exact_read_evidence_ids": not_exact_read,
                "next_action": (
                    "Call read_evidence or read_source_window for each listed evidence item in the current run, "
                    "then retry with the returned exact-read evidence_ids. Search/skim previews are not support."
                ),
            }
        return None

    async def _select_validated_evidence_ids(
        self,
        *,
        evidence_ids: list[str],
        purpose: str,
        role: str,
        rationale: str,
    ) -> dict[str, Any] | None:
        if not evidence_ids:
            return None
        selector = getattr(self.evidence_toolset, "_select_evidence_ids_for_write", None)
        if not callable(selector):
            return {
                "accepted": False,
                "error": "Evidence selection is unavailable; evidence-backed triage state was not persisted",
                "invalid_evidence_ids": evidence_ids,
            }
        invalid = await selector(
            evidence_ids,
            purpose=purpose,
            role=role,
            rationale=rationale,
        )
        if invalid:
            return {
                "accepted": False,
                "error": "evidence_ids could not be selected for the persisted triage status",
                "invalid_evidence_ids": invalid,
            }
        return None

    @staticmethod
    def _recompute_source_set_question_links(
        workspace: dict[str, Any],
        source_set: dict[str, Any],
    ) -> None:
        """Derive emergent question links from current assessments, not history."""

        set_id = str(source_set.get("set_id") or "")
        if "planned_question_ids" in source_set:
            planned_question_ids = workspace_api._dedupe(
                [str(item) for item in source_set.get("planned_question_ids") or []]
            )
        else:
            stored_cross_ids = {
                str(item) for item in source_set.get("cross_question_ids") or []
            }
            planned_question_ids = workspace_api._dedupe(
                [
                    str(item)
                    for item in source_set.get("question_ids") or []
                    if str(item) not in stored_cross_ids
                ]
            )
        assessment_question_ids = workspace_api._dedupe(
            [
                str(question_id)
                for assessment in workspace.get("source_assessments") or []
                if isinstance(assessment, dict) and str(assessment.get("set_id") or "") == set_id
                for question_id in assessment.get("coverage_question_ids") or []
            ]
        )
        cross_question_ids = [
            question_id
            for question_id in assessment_question_ids
            if question_id not in set(planned_question_ids)
        ]
        previous_question_ids = {
            str(item) for item in source_set.get("question_ids") or []
        }
        source_set["planned_question_ids"] = planned_question_ids
        source_set["cross_question_ids"] = cross_question_ids
        source_set["question_ids"] = workspace_api._dedupe(
            [*planned_question_ids, *cross_question_ids]
        )
        removed_question_ids = previous_question_ids - set(source_set["question_ids"])
        if removed_question_ids:
            workspace["coverage_outcomes"] = [
                item
                for item in workspace.get("coverage_outcomes") or []
                if not isinstance(item, dict)
                or str(item.get("set_id") or "") != set_id
                or str(item.get("question_id") or "") not in removed_question_ids
            ]

    @staticmethod
    def _record_pending_discovered_question_attempt(
        *,
        source_set: dict[str, Any],
        question_id: str,
        finding_ids: list[str],
        findings_by_id: dict[str, dict[str, Any]],
        requirement_ids: list[str],
    ) -> None:
        existing = next(
            (
                item
                for item in source_set.get("pending_discovered_question_attempts") or []
                if isinstance(item, dict) and str(item.get("question_id") or "") == question_id
            ),
            None,
        )
        combined_finding_ids = workspace_api._dedupe(
            [
                *(
                    [str(item) for item in existing.get("finding_ids") or []]
                    if isinstance(existing, dict)
                    else []
                ),
                *finding_ids,
            ]
        )
        source_refs = workspace_api._dedupe(
            [
                str(findings_by_id[finding_id].get("source_ref") or "")
                for finding_id in combined_finding_ids
                if finding_id in findings_by_id
                and str(findings_by_id[finding_id].get("source_ref") or "")
            ]
        )
        attempt = {
            "question_id": question_id,
            "requirement_ids": workspace_api._dedupe(
                [
                    *(
                        [str(item) for item in existing.get("requirement_ids") or []]
                        if isinstance(existing, dict)
                        else []
                    ),
                    *requirement_ids,
                ]
            ),
            "finding_ids": combined_finding_ids,
            "source_refs": source_refs,
            "reviewed_source_refs": (
                [str(item) for item in existing.get("reviewed_source_refs") or []]
                if isinstance(existing, dict)
                else []
            ),
            "updated_at": workspace_api._now(),
        }
        source_set["pending_discovered_question_attempts"] = [
            item
            for item in source_set.get("pending_discovered_question_attempts") or []
            if not isinstance(item, dict) or str(item.get("question_id") or "") != question_id
        ]
        source_set["pending_discovered_question_attempts"].append(attempt)

    @staticmethod
    def _reconcile_pending_discovered_question_attempts(
        *,
        workspace: dict[str, Any],
        source_set: dict[str, Any],
        reviewed_source_ref: str | None,
    ) -> None:
        set_id = str(source_set.get("set_id") or "")
        findings = [
            item
            for item in workspace.get("source_findings") or []
            if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
        ]
        pending: list[dict[str, Any]] = []
        for raw_attempt in source_set.get("pending_discovered_question_attempts") or []:
            if not isinstance(raw_attempt, dict):
                continue
            attempt = dict(raw_attempt)
            question_id = str(attempt.get("question_id") or "")
            attempted_finding_ids = {str(item) for item in attempt.get("finding_ids") or []}
            linked = any(
                str(finding.get("finding_id") or "") in attempted_finding_ids
                and _finding_supports_question(finding, question_id)
                for finding in findings
            )
            if linked:
                # The durable finding link now makes this ordinary discovered
                # set work; question_ids/outcome completeness enforce the rest.
                continue
            reviewed_refs = {str(item) for item in attempt.get("reviewed_source_refs") or []}
            source_refs = {str(item) for item in attempt.get("source_refs") or []}
            if reviewed_source_ref and reviewed_source_ref in source_refs:
                reviewed_refs.add(reviewed_source_ref)
            if source_refs and source_refs.issubset(reviewed_refs):
                # Every owner source was explicitly reviewed and accepted again
                # after the rejected attempt without the proposed link. A
                # semantic no-op is valid confirmation; no invented change is
                # required to clear an erroneous attempted link.
                continue
            attempt["reviewed_source_refs"] = sorted(reviewed_refs)
            pending.append(attempt)
        source_set["pending_discovered_question_attempts"] = pending

    @staticmethod
    def _recompute_all_set_evidence_ids(workspace: dict[str, Any]) -> None:
        evidence_by_set: dict[str, list[str]] = {}
        for field in ("source_assessments", "source_findings", "coverage_outcomes"):
            for item in workspace.get(field) or []:
                if not isinstance(item, dict):
                    continue
                set_id = str(item.get("set_id") or "")
                if set_id:
                    evidence_by_set.setdefault(set_id, []).extend(
                        str(value) for value in item.get("evidence_ids") or []
                    )
                    if field == "source_findings":
                        evidence_by_set[set_id].extend(
                            str(value)
                            for relation in item.get("source_relations") or []
                            if isinstance(relation, dict)
                            for value in relation.get("evidence_ids") or []
                        )
        recomputed_sets: list[dict[str, Any]] = []
        for raw_source_set in workspace.get("source_sets") or []:
            source_set = dict(raw_source_set)
            set_id = str(source_set.get("set_id") or "")
            evidence_ids = list(evidence_by_set.get(set_id, []))
            for field in ("observations", "written_actions"):
                for item in source_set.get(field) or []:
                    if isinstance(item, dict):
                        evidence_ids.extend(str(value) for value in item.get("evidence_ids") or [])
            source_set["evidence_ids"] = workspace_api._dedupe(evidence_ids)
            recomputed_sets.append(source_set)
        workspace["source_sets"] = recomputed_sets

    @staticmethod
    def _assessment_deferral_reason(assessment: dict[str, Any], *, fallback: str = "") -> str:
        return next(
            (
                value
                for value in (
                    str(assessment.get("disposition_reason") or "").strip(),
                    str(assessment.get("representation_reason") or "").strip(),
                    str(assessment.get("relevance_reason") or "").strip(),
                    str(assessment.get("uniqueness_reason") or "").strip(),
                    fallback.strip(),
                )
                if value
            ),
            "",
        )

    @classmethod
    def _sync_source_resolution_indexes(
        cls,
        workspace: dict[str, Any],
        *,
        reason_overrides: dict[str, str] | None = None,
    ) -> None:
        overrides = reason_overrides or {}
        existing_reasons = {
            str(item.get("source_ref") or ""): str(item.get("reason") or "").strip()
            for item in workspace.get("deferred_sources") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
        }
        set_reason_by_ref = {
            str(source_ref): str(source_set.get("deferred_reason") or "").strip()
            for source_set in workspace.get("source_sets") or []
            if isinstance(source_set, dict)
            for source_ref in source_set.get("source_refs") or []
        }
        assessment_by_ref = {
            str(item.get("source_ref") or ""): item
            for item in workspace.get("source_assessments") or []
            if isinstance(item, dict) and str(item.get("source_ref") or "").strip()
        }
        deferred_entries: list[dict[str, str]] = []
        for source_ref in workspace.get("source_order") or []:
            source_ref = str(source_ref)
            assessment = assessment_by_ref.get(source_ref)
            if not assessment or str(assessment.get("disposition") or "") != "deferred":
                continue
            reason = (
                str(overrides.get(source_ref) or "").strip()
                or cls._assessment_deferral_reason(
                    assessment,
                    fallback=set_reason_by_ref.get(source_ref, ""),
                )
                or existing_reasons.get(source_ref, "")
            )
            deferred_entries.append({"source_ref": source_ref, "reason": reason})
        workspace["deferred_sources"] = deferred_entries

        deferred_refs = {item["source_ref"] for item in deferred_entries}
        mapped_refs: list[str] = []
        for source_set in workspace.get("source_sets") or []:
            if not isinstance(source_set, dict) or str(source_set.get("status") or "") != "mapped":
                continue
            mapped_refs.extend(
                source_ref
                for source_ref in workspace_api._source_set_covered_refs(source_set)
                if source_ref not in deferred_refs
            )
        workspace["mapped_sources"] = workspace_api._dedupe(mapped_refs)

    async def finish_triage_source_set_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(
            args.get("set_id") or self.current_set_id or (current.get("current_set") or {}).get("set_id") or ""
        )
        source_set = public_api._workspace_set(current, set_id)
        if source_set is None:
            return {"accepted": False, "error": "Unknown source triage set", "set_id": set_id}
        set_kind = str(source_set.get("set_kind") or SOURCE_SET_KIND_COVERAGE)
        if set_kind not in {SOURCE_SET_KIND_COVERAGE, SOURCE_SET_KIND_ORIENTATION}:
            return {"accepted": False, "error": "Unknown source set kind", "set_kind": set_kind}
        planned_question_ids = [
            str(item) for item in source_set.get("planned_question_ids", source_set.get("question_ids") or [])
        ]
        if set_kind == SOURCE_SET_KIND_COVERAGE and not planned_question_ids and not source_set.get("repair"):
            return {
                "accepted": False,
                "error": "A coverage source set requires at least one planned coverage question",
            }
        if set_kind == SOURCE_SET_KIND_ORIENTATION and planned_question_ids:
            return {
                "accepted": False,
                "error": "An initial-reading source set cannot have planner-assigned coverage questions",
                "question_ids": planned_question_ids,
            }
        status = str(args.get("status") or "").strip()
        if status not in {"mapped", "deferred"}:
            return {
                "accepted": False,
                "error": "status must be mapped or deferred",
                "next_action": "Persist the unfinished source analyses and outcomes, then retry this terminal tool.",
            }
        summary = str(args.get("summary") or "").strip()
        if not summary:
            return {"accepted": False, "error": "summary is required"}
        ref_context = build_source_ref_context(self.inventory)
        source_ids_by_ref = {
            str(ref): resolve_source_ref(str(ref), ref_context) for ref in source_set.get("source_refs") or []
        }
        assessments_by_source = {
            str(item.get("source_id") or ""): item
            for item in current.get("source_assessments") or []
            if isinstance(item, dict) and item.get("set_id") == set_id
        }
        assessments_by_ref = {
            ref: assessments_by_source[source_id]
            for ref, source_id in source_ids_by_ref.items()
            if source_id and source_id in assessments_by_source
        }
        missing_assessment_refs = [
            ref
            for ref, source_id in source_ids_by_ref.items()
            if not source_id or source_id not in assessments_by_source
        ]
        if missing_assessment_refs:
            return {
                "accepted": False,
                "error": "Every source in the set needs a persisted source assessment",
                "missing_source_refs": missing_assessment_refs,
            }
        missing_analysis_refs = sorted(
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if not assessment.get("finding_ids")
            and not str(assessment.get("no_findings_reason") or "").strip()
        )
        if missing_analysis_refs:
            return {
                "accepted": False,
                "error": "Every source needs durable findings or an explicit no_findings_reason",
                "missing_source_refs": missing_analysis_refs,
                "next_action": "Read each source and call save_source_analysis.",
            }
        unfinished_assessment_refs = sorted(
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if assessment.get("processing_status") is not None
            and str(assessment.get("processing_status")) != "resolved"
        )
        if unfinished_assessment_refs:
            return {
                "accepted": False,
                "error": "mapped/deferred set requires completed source analyses",
                "source_refs": unfinished_assessment_refs,
                "next_action": "Complete and persist these source analyses, then retry finish_triage_source_set.",
            }
        pending_discovered_attempts = [
            item
            for item in source_set.get("pending_discovered_question_attempts") or []
            if isinstance(item, dict) and str(item.get("question_id") or "")
        ]
        if pending_discovered_attempts:
            return {
                "accepted": False,
                "error": "Rejected discovered-question lineage attempts still need a durable correction",
                "pending_question_attempts": [
                    {
                        "question_id": str(item.get("question_id") or ""),
                        "requirement_ids": [str(value) for value in item.get("requirement_ids") or []],
                        "finding_ids": [str(value) for value in item.get("finding_ids") or []],
                        "source_refs": [str(value) for value in item.get("source_refs") or []],
                        "unreviewed_source_refs": sorted(
                            {str(value) for value in item.get("source_refs") or []}
                            - {str(value) for value in item.get("reviewed_source_refs") or []}
                        ),
                    }
                    for item in pending_discovered_attempts
                ],
                "next_action": (
                    "For each pending question, resave the listed owner source with the intended exact "
                    "question/requirement links and then save its outcome. If the attempted link was wrong, "
                    "resave every listed owner source with its complete reviewed analysis; an identical accepted "
                    "analysis is valid confirmation that no such link exists."
                ),
            }
        outcomes_by_question = {
            str(item.get("question_id") or ""): item
            for item in current.get("coverage_outcomes") or []
            if isinstance(item, dict) and item.get("set_id") == set_id
        }
        missing_outcome_ids = [
            str(question_id)
            for question_id in source_set.get("question_ids") or []
            if str(question_id) not in outcomes_by_question
        ]
        if missing_outcome_ids:
            return {
                "accepted": False,
                "error": "Every assigned coverage question needs a final outcome",
                "missing_question_ids": missing_outcome_ids,
            }
        stale_outcome_ids = sorted(
            question_id
            for question_id, outcome in outcomes_by_question.items()
            if bool(outcome.get("is_stale"))
        )
        if stale_outcome_ids:
            return {
                "accepted": False,
                "error": "Every question outcome must be refreshed after later linked findings",
                "stale_question_ids": stale_outcome_ids,
                "next_action": "Update these outcomes from the current saved findings.",
            }
        has_representation_blocker = any(
            str(assessment.get("representation_status") or "")
            in {"partial", "not_target_content", "unreadable"}
            for assessment in assessments_by_ref.values()
        )
        unjustified_deferred_question_ids = sorted(
            question_id
            for question_id, outcome in outcomes_by_question.items()
            if (
                str(outcome.get("status") or "") == "deferred"
                or any(
                    isinstance(result, dict)
                    and str(result.get("status") or "") == "deferred"
                    for result in outcome.get("requirement_results") or []
                )
            )
            and not has_representation_blocker
        )
        if unjustified_deferred_question_ids:
            return {
                "accepted": False,
                "error": "Deferred outcomes no longer have a checked representation blocker",
                "question_ids": unjustified_deferred_question_ids,
                "next_action": (
                    "Refresh these outcomes from the current source analyses, using missing only for checked absence."
                ),
            }
        if status == "mapped":
            has_semantic_state = bool(
                source_set.get("observations")
                or source_set.get("written_actions")
                or source_set.get("evidence_ids")
                or assessments_by_source
                or outcomes_by_question
            )
            if not has_semantic_state:
                return {
                    "accepted": False,
                    "error": "mapped set requires at least one observation, write action, or evidence_id",
                }
        if status == "deferred" and not str(args.get("deferred_reason") or "").strip():
            return {"accepted": False, "error": "deferred_reason is required for deferred status"}
        deferred_reason = str(args.get("deferred_reason") or "").strip()
        if status == "mapped" and deferred_reason:
            return {
                "accepted": False,
                "error": "mapped set must keep deferred_reason empty; use deferred_sources for individual sources",
            }
        deferred_sources = []
        for item in args.get("deferred_sources") or []:
            if not isinstance(item, dict):
                continue
            source_ref = str(item.get("source_ref") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if source_ref not in set(source_set.get("source_refs") or []):
                return {
                    "accepted": False,
                    "error": "deferred source_ref must belong to the current source set",
                    "source_ref": source_ref,
                }
            if not reason:
                return {"accepted": False, "error": "Each deferred source requires a reason", "source_ref": source_ref}
            reason_error = semantic_field_error(
                {"deferred_reason": reason}, TRIAGE_SEMANTIC_FIELD_LIMITS, ["deferred_reason"]
            )
            if reason_error:
                return reason_error
            deferred_sources.append({"source_ref": source_ref, "reason": reason})
        requested_deferred_reasons = {item["source_ref"]: item["reason"] for item in deferred_sources}
        assessment_deferred_refs = {
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if str(assessment.get("disposition") or "") == "deferred"
        }
        contradictory_deferred_refs = sorted(set(requested_deferred_reasons) - assessment_deferred_refs)
        if contradictory_deferred_refs:
            return {
                "accepted": False,
                "error": "A deferred source must have disposition=deferred in its persisted assessment",
                "source_refs": contradictory_deferred_refs,
            }
        if status == "deferred":
            nondeferred_assessment_refs = sorted(set(source_ids_by_ref) - assessment_deferred_refs)
            if nondeferred_assessment_refs:
                return {
                    "accepted": False,
                    "error": "A deferred set requires disposition=deferred for every source assessment",
                    "source_refs": nondeferred_assessment_refs,
                }
            active_outcome_question_ids = sorted(
                question_id
                for question_id, outcome in outcomes_by_question.items()
                if str(outcome.get("status") or "") != "deferred"
            )
            if active_outcome_question_ids:
                return {
                    "accepted": False,
                    "error": "A deferred set requires deferred outcomes for all assigned questions",
                    "question_ids": active_outcome_question_ids,
                }

        ineligible_assessment_refs = {
            source_ref
            for source_ref, assessment in assessments_by_ref.items()
            if str(assessment.get("representation_status") or "unchecked") not in {"readable", "partial"}
            or str(assessment.get("relevance") or "unknown") == "out_of_scope"
            or str(assessment.get("disposition") or "background") in {"unusable", "deferred"}
        }
        active_outcomes_using_ineligible: list[str] = []
        for question_id, outcome in outcomes_by_question.items():
            if str(outcome.get("status") or "") not in {"covered", "partial", "conflicted"}:
                continue
            outcome_refs = {str(item) for item in outcome.get("source_refs") or []}
            outcome_source_ids = {str(item) for item in outcome.get("source_ids") or []}
            outcome_refs.update(ref for ref, source_id in source_ids_by_ref.items() if source_id in outcome_source_ids)
            if outcome_refs.intersection(ineligible_assessment_refs):
                active_outcomes_using_ineligible.append(question_id)
        if active_outcomes_using_ineligible:
            return {
                "accepted": False,
                "error": (
                    "Covered, partial, or conflicted outcomes cannot use not-target-content, unreadable, "
                    "out-of-scope, unusable, or deferred sources"
                ),
                "question_ids": sorted(active_outcomes_using_ineligible),
            }
        if status == "mapped":
            assessment_refs = set(assessments_by_ref) - assessment_deferred_refs
            covered_refs = {
                *public_api._source_set_covered_refs(source_set),
                *assessment_refs,
            } - assessment_deferred_refs
            unresolved_refs = [
                str(ref)
                for ref in source_set.get("source_refs") or []
                if str(ref) not in covered_refs and str(ref) not in assessment_deferred_refs
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
        source_set["assessment_source_refs"] = [ref for ref in source_ids_by_ref if ref not in assessment_deferred_refs]
        source_set["deferred_source_refs"] = [ref for ref in source_ids_by_ref if ref in assessment_deferred_refs]
        source_set["outcome_question_ids"] = list(outcomes_by_question)
        source_set["updated_at"] = public_api._now()
        public_api._replace_workspace_set(current, source_set)
        processed = [item for item in current.get("processed_sets") or [] if item.get("set_id") != set_id]
        processed.append(
            {
                "set_id": set_id,
                "status": status,
                "summary": source_set["findings_summary"],
                "updated_at": public_api._now(),
            }
        )
        current["processed_sets"] = processed
        current["current_set"] = None
        if status in {"mapped", "deferred"}:
            for field in (
                "repair_baseline_source_assessments",
                "repair_baseline_coverage_outcomes",
            ):
                if field in current:
                    current[field] = [
                        item
                        for item in current.get(field) or []
                        if not isinstance(item, dict) or str(item.get("set_id") or "") != set_id
                    ]
        deferral_reason_overrides = dict(requested_deferred_reasons)
        if status == "deferred":
            deferral_reason_overrides.update(
                {
                    str(ref): deferred_reason or summary
                    for ref in source_set.get("source_refs") or []
                    if str(ref) not in deferral_reason_overrides
                }
            )
        self._sync_source_resolution_indexes(current, reason_overrides=deferral_reason_overrides)
        invalidate_coverage_syntheses(current)
        await public_api.save_source_triage_workspace(self.db, generation_run_id=self.run.id, workspace=current)
        return {"accepted": True, "set_id": set_id, "status": status, "processed_set_count": len(processed)}


__all__ = ["SourceTriageWorkspaceToolHandlers"]


def _confidence(value: Any) -> float:
    return max(0.0, min(float(0.5 if value is None else value), 1.0))


def _semantic_json(value: Any) -> str:
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: clean(child)
                for key, child in item.items()
                if key not in {"updated_at", "semantic_revision"}
            }
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _finding_supports_question(finding: dict[str, Any], question_id: str) -> bool:
    return any(
        isinstance(link, dict) and str(link.get("question_id") or "") == question_id
        for link in finding.get("question_links") or []
    )


def _outcome_argument_finding_ids(args: dict[str, Any]) -> list[str]:
    return workspace_api._dedupe(
        [
            *[str(item) for item in args.get("finding_ids") or [] if str(item)],
            *[
                str(finding_id)
                for result in args.get("requirement_results") or []
                if isinstance(result, dict)
                for finding_id in result.get("finding_ids") or []
                if str(finding_id)
            ],
        ]
    )


def _outcome_argument_requirement_ids(args: dict[str, Any]) -> list[str]:
    return workspace_api._dedupe(
        [
            str(result.get("requirement_id") or "")
            for result in args.get("requirement_results") or []
            if isinstance(result, dict) and str(result.get("requirement_id") or "")
        ]
    )


def _outcome_argument_lineage_errors(
    *,
    args: dict[str, Any],
    allowed_finding_ids: set[str],
    findings_by_id: dict[str, dict[str, Any]],
    set_id: str,
    question_id: str,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    requirement_finding_ids: set[str] = set()
    for result in args.get("requirement_results") or []:
        if not isinstance(result, dict):
            continue
        requirement_id = str(result.get("requirement_id") or "")
        finding_ids = [
            str(item)
            for item in result.get("finding_ids") or []
            if str(item) in allowed_finding_ids
        ]
        requirement_finding_ids.update(finding_ids)
        errors.extend(
            _finding_lineage_errors(
                finding_ids=finding_ids,
                findings_by_id=findings_by_id,
                set_id=set_id,
                question_id=question_id,
                requirement_id=requirement_id or None,
            )
        )
    top_level_only = [
        finding_id
        for finding_id in _outcome_argument_finding_ids(args)
        if finding_id in allowed_finding_ids and finding_id not in requirement_finding_ids
    ]
    errors.extend(
        _finding_lineage_errors(
            finding_ids=top_level_only,
            findings_by_id=findings_by_id,
            set_id=set_id,
            question_id=question_id,
        )
    )
    return errors


def _finding_lineage_errors(
    *,
    finding_ids: list[str],
    findings_by_id: dict[str, dict[str, Any]],
    set_id: str | None,
    question_id: str,
    requirement_id: str | None = None,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for finding_id in finding_ids:
        finding = findings_by_id.get(finding_id)
        if finding is None:
            errors.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement_id or "",
                    "finding_id": finding_id,
                    "source_ref": "",
                    "reason": "unknown_finding",
                    "corrective_action": "Page saved source_findings and copy an exact current finding_id.",
                }
            )
            continue
        source_ref = str(finding.get("source_ref") or "")
        finding_set_id = str(finding.get("set_id") or "")
        if set_id is not None and finding_set_id != set_id:
            errors.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement_id or "",
                    "finding_id": finding_id,
                    "source_ref": source_ref,
                    "finding_set_id": finding_set_id,
                    "required_set_id": set_id,
                    "reason": "finding_from_another_set",
                    "corrective_action": "Use a finding owned by the current set; do not copy cross-set lineage.",
                }
            )
            continue
        linked = (
            _finding_supports_requirement(finding, question_id, requirement_id)
            if requirement_id
            else _finding_supports_question(finding, question_id)
        )
        if not linked:
            errors.append(
                {
                    "question_id": question_id,
                    "requirement_id": requirement_id or "",
                    "finding_id": finding_id,
                    "source_ref": source_ref,
                    "reason": (
                        "finding_not_linked_to_requirement"
                        if requirement_id
                        else "finding_not_linked_to_question"
                    ),
                    "corrective_action": (
                        "Resave this source's complete analysis with the finding linked to the exact question and "
                        "requirement; otherwise remove this finding from the outcome."
                    ),
                }
            )
    return errors


def _lineage_repair_next_action(lineage_errors: list[dict[str, Any]]) -> str:
    if not lineage_errors:
        return "Retry with the exact current-set findings linked to this question requirement."
    if any(str(item.get("reason") or "").startswith("finding_not_linked") for item in lineage_errors):
        return (
            "For each listed source_ref, resave save_source_analysis with the finding's exact question_id and "
            "requirement_ids, then retry; otherwise remove the offending finding_id from this outcome."
        )
    if any(item.get("reason") == "unknown_finding" for item in lineage_errors):
        return "Page the current saved state, copy exact finding_ids, and retry."
    return "Replace findings from another set with linked findings owned by this set, then retry."


def _finding_supports_requirement(
    finding: dict[str, Any],
    question_id: str,
    requirement_id: str,
) -> bool:
    return any(
        isinstance(link, dict)
        and str(link.get("question_id") or "") == question_id
        and requirement_id in {str(item) for item in link.get("requirement_ids") or []}
        for link in finding.get("question_links") or []
    )


def _local_requirement_ids(
    *,
    question: dict[str, Any],
    source_set: dict[str, Any],
    findings_by_id: dict[str, dict[str, Any]],
    question_id: str,
) -> set[str]:
    known_ids = {
        str(item.get("requirement_id") or "")
        for item in question.get("answer_requirements") or []
        if isinstance(item, dict) and str(item.get("requirement_id") or "")
    }
    assigned_ids: set[str] = set()
    assignment_found = False
    for assignment in source_set.get("question_assignments") or []:
        if not isinstance(assignment, dict):
            continue
        if str(assignment.get("question_id") or "") != question_id:
            continue
        assignment_found = True
        assigned_ids.update(
            str(value) for value in assignment.get("requirement_ids") or [] if str(value)
        )
    planned_question_ids = {
        str(value)
        for value in source_set.get("planned_question_ids", source_set.get("question_ids") or [])
    }
    if not assignment_found and question_id in planned_question_ids:
        # Completed V2 work before explicit requirement assignments treated a
        # planned question as whole-question responsibility.
        assigned_ids.update(known_ids)
    set_id = str(source_set.get("set_id") or "")
    linked_ids = {
        str(requirement_id)
        for finding in findings_by_id.values()
        if str(finding.get("set_id") or "") == set_id
        for link in finding.get("question_links") or []
        if isinstance(link, dict) and str(link.get("question_id") or "") == question_id
        for requirement_id in link.get("requirement_ids") or []
        if str(requirement_id)
    }
    return (assigned_ids | linked_ids) & known_ids


def _requirement_status_consistency_error(
    *,
    overall_status: str,
    requirement_statuses: list[str],
) -> dict[str, Any] | None:
    statuses = set(requirement_statuses)
    consistent = {
        "covered": statuses == {"covered"},
        "missing": statuses == {"missing"},
        "deferred": bool(statuses) and "deferred" in statuses and statuses.issubset({"missing", "deferred"}),
        "conflicted": "conflicted" in statuses,
        "partial": (
            bool(statuses)
            and "conflicted" not in statuses
            and not statuses.issubset({"covered"})
            and not statuses.issubset({"missing"})
            and not ("deferred" in statuses and statuses.issubset({"missing", "deferred"}))
        ),
    }.get(overall_status, False)
    if consistent:
        return None
    return {
        "accepted": False,
        "error": "Overall coverage status contradicts saved requirement results",
        "overall_status": overall_status,
        "requirement_statuses": requirement_statuses,
        "next_action": "Correct the semantic result statuses or the overall status and retry.",
    }


def _question_next_action_from_requirements(
    *,
    requirement_results: list[dict[str, Any]],
    fallback: str,
) -> str:
    """Aggregate structured routes while preserving the legacy question-level contract."""

    if not requirement_results:
        return fallback
    actions = {str(item.get("next_action") or "none") for item in requirement_results}
    for action in ("source_acquisition", "research", "omit", "none"):
        if action in actions:
            return action
    return "none"
