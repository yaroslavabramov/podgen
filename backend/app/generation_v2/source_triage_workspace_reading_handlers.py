from __future__ import annotations

import hashlib
from typing import Any

from app.generation_v2 import source_triage_workspace as workspace_api
from app.generation_v2.runtime_tool_output import MODEL_TOOL_OUTPUT_MAX_CHARS
from app.generation_v2.source_triage_workspace_handler_helpers import pending_set_work
from app.generation_v2.source_triage_language_contract import reading_note_language_error
from app.generation_v2.source_triage_workspace_state import source_read_receipt_is_delivered
from app.generation_v2.tools.evidence_refs import build_source_ref_context, resolve_source_ref
from app.generation_v2.tools.evidence_source_text import source_version_text


_READING_NOTE_SCALAR_MAX_CHARS = MODEL_TOOL_OUTPUT_MAX_CHARS - 1_200


class SourceTriageWorkspaceReadingHandlersMixin:
    async def save_reading_note_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Save semantic working memory only for ranges actually returned to this set agent."""

        public_api = workspace_api
        current = await public_api.load_source_triage_workspace(self.db, self.run.id) or self.workspace
        set_id = str(self.current_set_id or (current.get("current_set") or {}).get("set_id") or "")
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

        language_error = reading_note_language_error(
            args,
            output_language=str((current.get("intent") or {}).get("language") or ""),
        )
        if language_error:
            return language_error

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
        current_version_ids = {str(value) for value in (getattr(inventory_card, "source_version_ids", []) or [])}
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
                if attachment_start != 0 or attachment_end != 0 or not pinned_hash or receipt_hash != pinned_hash:
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
                str(cached_text_getter(version)) if callable(cached_text_getter) else source_version_text(version)
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
            (field, None, value)
            for field, value in (
                ("what_was_read", str(args.get("what_was_read") or "")),
                ("what_was_established", str(args.get("what_was_established") or "")),
                ("boundaries", str(args.get("boundaries") or "")),
                ("unread_regions", str(args.get("unread_regions") or "")),
                (
                    "unread_risk_assessment",
                    str(
                        args.get("unread_risk_assessment")
                        or args.get("why_unread_is_safe")
                        or ""
                    ),
                ),
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
            + hashlib.sha256("|".join([set_id, source_ref, *receipt_refs]).encode("utf-8")).hexdigest()[:24]
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
            str(existing_note.get("set_id") or "") != set_id or str(existing_note.get("source_ref") or "") != source_ref
        ):
            return {
                "accepted": False,
                "error": "note_id belongs to another source or set",
                "note_id": note_id,
            }

        now = public_api._now()
        unread_risk_assessment = str(
            args.get("unread_risk_assessment")
            or args.get("why_unread_is_safe")
            or ""
        )
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
            "unread_risk_assessment": unread_risk_assessment,
            **(
                {"why_unread_is_safe": unread_risk_assessment}
                if "why_unread_is_safe" in args and "unread_risk_assessment" not in args
                else {}
            ),
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
            **pending_set_work(current, source_set),
        }


__all__ = ["SourceTriageWorkspaceReadingHandlersMixin"]
