from __future__ import annotations

import hashlib
from typing import Any

from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.contracts import V2AgentName, V2Phase, V2ToolName
from app.generation_v2.model_context_text import model_preview_text
from app.generation_v2.source_triage_model_delivery import (
    source_structure_receipt_ref,
    stage_source_structure_page,
)
from app.generation_v2.source_triage_exact_duplicates import (
    exact_duplicate_predecessor,
)
from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)
from app.generation_v2.source_triage_workspace_state import (
    READ_RECEIPT_DELIVERY_DELIVERED,
    READ_RECEIPT_DELIVERY_PENDING,
    now as workspace_now,
    source_read_receipt_is_delivered,
)
from app.generation_v2.tools.evidence_model_window_refs import (
    register_model_window_ref,
    resolve_model_window_ref,
)
from app.generation_v2.tools.evidence_receipt_selection import (
    select_receipt_evidence_range,
)
from app.generation_v2.tools.evidence_search_context import expanded_search_context
from app.generation_v2.tools.evidence_source_structure import (
    build_source_structure_page,
)
from app.generation_v2.tools.evidence_source_passages import (
    resolve_source_passage_ref,
    source_passages_for_range,
)
from app.generation_v2.tools.evidence_source_passage_handles import (
    attach_source_passage_handles,
    passage_ref_repair_payload,
    validate_delivered_passage_refs,
)
from app.generation_v2.tools.evidence_source_tool_actions import (
    search_text_source_windows as _search_text_source_windows_action,
    skim_source as _skim_source_action,
)
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_scope import target_aware_triage_read_guard
from app.generation_v2.tools.evidence_utils import (
    _normalize,
)
from app.generation_v2.tools.evidence_window_refs import (
    WindowRefError,
    create_window_ref,
    is_window_ref,
    parse_window_ref,
)
from app.models.generation_v2 import EvidenceLocatorType, EvidenceStrength


SOURCE_NAVIGATION_READ_MIN_CHARS = 1_000
SOURCE_NAVIGATION_READ_MAX_CHARS = 20_000
SOURCE_NAVIGATION_SEARCH_MIN_CHARS = 400
SOURCE_NAVIGATION_SEARCH_MAX_CHARS = 3_000
SOURCE_NAVIGATION_SEARCH_MAX_RESULTS = 8
SOURCE_NAVIGATION_QUERY_MAX_CHARS = 1_000
SOURCE_NAVIGATION_LOCATOR_PREVIEW_CHARS = 400


def _source_version_text(version: Any) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


def _owner_source_version_text(owner: Any, version: Any) -> str:
    cached_getter = getattr(owner, "_cached_source_version_text", None)
    if callable(cached_getter):
        return str(cached_getter(version))
    return _source_version_text(version)


def _owner_source_version_text_error_payload(
    owner: Any,
    version: Any,
) -> dict[str, Any] | None:
    getter = getattr(owner, "_source_version_text_error_payload", None)
    if not callable(getter):
        return None
    payload = getter(version)
    return dict(payload) if isinstance(payload, dict) and payload.get("error") else None


def _validated_int(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> tuple[int, dict[str, Any] | None]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0, {
            "error": f"{field}_must_be_integer",
            "minimum": minimum,
            "maximum": maximum,
        }
    if parsed < minimum or parsed > maximum:
        return 0, {
            "error": f"{field}_out_of_range",
            "minimum": minimum,
            "maximum": maximum,
            "received": parsed,
        }
    return parsed, None


def _model_range(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_offset": int(item["start_offset"]),
        "end_offset": int(item["end_offset"]),
        "offset_unit": "unicode_codepoint",
        "end_exclusive": True,
    }


def _model_navigation_locator(value: Any) -> dict[str, Any]:
    text = model_preview_text(value)
    preview = text[:SOURCE_NAVIGATION_LOCATOR_PREVIEW_CHARS]
    return {
        "locator": preview,
        "locator_chars": len(text),
        "locator_omitted_chars": max(0, len(text) - len(preview)),
    }


def _source_read_receipt_ref(
    *,
    set_id: str,
    source_ref: str,
    source_version_id: str,
    content_sha256: str,
    start_offset: int,
    end_offset: int,
    read_kind: str,
) -> str:
    identity = "\x1f".join(
        [
            set_id,
            source_ref,
            source_version_id,
            content_sha256,
            str(start_offset),
            str(end_offset),
            read_kind,
        ]
    )
    return f"receipt:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


class EvidenceSourceToolsMixin:
    async def read_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read exact contiguous source text for set-agent reasoning, without evidence side effects."""

        requested_source_ref = str(args.get("source_ref") or "").strip()
        if not requested_source_ref:
            return {"found": False, "error": "source_ref_required"}
        max_chars, size_error = _validated_int(
            args.get("max_chars"),
            field="max_chars",
            minimum=SOURCE_NAVIGATION_READ_MIN_CHARS,
            maximum=SOURCE_NAVIGATION_READ_MAX_CHARS,
        )
        if size_error:
            return {"found": False, **size_error}
        guard_error = await target_aware_triage_read_guard(
            self,
            target_source_refs=[requested_source_ref],
            exact_read=False,
        )
        if guard_error:
            return guard_error
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=requested_source_ref,
            version_ref="",
        )
        if ref_error:
            return {"found": False, **ref_error}
        version = await self._resolve_source_version(
            source_id="",
            source_version_id="",
            source_ref=requested_source_ref,
            version_ref="",
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        source_ref = await self._source_ref_for_id(version.source_id)
        cursor = str(args.get("cursor") or "").strip()
        resolved_cursor = await self._resolve_source_read_cursor(
            version=version,
            source_ref=source_ref,
            cursor=cursor,
            max_chars=max_chars,
        )
        if "error" in resolved_cursor:
            return {"found": False, **resolved_cursor}
        try:
            page = await self._read_source_text_page(
                version,
                cursor=str(resolved_cursor.get("canonical_cursor") or ""),
                max_chars=max_chars,
            )
        except ValueError as exc:
            return {
                "found": False,
                "error": str(exc),
                "next_action": f"Retry read_source with source_ref='{source_ref}' and cursor=''.",
            }
        if not page["text"]:
            load_error = _owner_source_version_text_error_payload(self, version)
            if load_error:
                return {"found": False, "source_ref": source_ref, **load_error}
            return {"found": False, "error": "Source version has no readable local text."}
        receipts = await self._persist_source_read_receipts(
            version=version,
            source_ref=source_ref,
            ranges=[page],
            read_kind="sequential",
            request_metadata={"cursor": cursor, "max_chars": max_chars},
        )
        if "error" in receipts:
            return {"found": False, **receipts}
        receipt_ref = receipts["receipt_refs"][0]
        next_cursor = receipt_ref if not page["eof"] else ""
        passages = receipts["passages_by_receipt_ref"][receipt_ref]
        exact_duplicate = await self._triage_exact_duplicate_predecessor(source_ref)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.CORPUS_EVIDENCE,
            agent_name=V2AgentName.CORPUS_EVIDENCE_API,
            tool_name=V2ToolName.READ_SOURCE,
            input_json={
                "source_ref": source_ref,
                "cursor": cursor,
                "max_chars": max_chars,
            },
            output_json={
                "receipt_ref": receipt_ref,
                "start_offset": page["start_offset"],
                "end_offset": page["end_offset"],
                "eof": page["eof"],
            },
        )
        return {
            "found": True,
            "source_ref": source_ref,
            "eof": page["eof"],
            "next_cursor": next_cursor,
            "receipt_ref": receipt_ref,
            "before_opening_different_source": {
                "required": True,
                "source_ref": source_ref,
                "receipt_refs_from_this_read": [receipt_ref],
                "allowed_next_actions": ["save_reading_note", "save_source_analysis"],
                "next_action": (
                    "Continue this source with next_cursor, or before opening a different source save a reading "
                    "note covering this and every earlier uncheckpointed sequential receipt for the source; a "
                    "complete source analysis is also valid."
                ),
            },
            "source_analysis_guidance": (
                {
                    "recommended_next_action": "save_source_analysis",
                    "reason": (
                        "This source has a byte-identical persisted representation in an earlier source. "
                        "Record it as readable with uniqueness=duplicate and the returned "
                        "duplicate_of_source_ref; do not classify a duplicate as not_target_content."
                    ),
                    "duplicate_of_source_ref": exact_duplicate["source_ref"],
                    "exact_duplicate_verified": True,
                }
                if exact_duplicate
                else (
                    {
                        "recommended_next_action": "save_source_analysis",
                        "reason": "Sequential reading reached EOF; synthesize this source before switching.",
                    }
                    if page["eof"]
                    else {
                        "recommended_next_action": "continue_read_source",
                        "reason": (
                            "More sequential text remains. Continue with next_cursor. If you deliberately stop "
                            "early, inspect every structure page and save a reading note before switching."
                        ),
                    }
                )
            ),
            **({"exact_duplicate": exact_duplicate} if exact_duplicate else {}),
            # Put controls before source text in the serialized result so they
            # are visible before a long passage payload.
            "passages": passages,
            "range": _model_range(page),
            "returned_chars": sum(len(item["text"]) for item in passages),
            "total_chars": page["total_chars"],
            "cursor": cursor,
            # The model sees this payload only after the provider received the
            # range. Durable state stays pending until that response succeeds.
            "delivery_status": READ_RECEIPT_DELIVERY_DELIVERED,
            "creates_evidence": False,
            "_runtime_receipt_delivery": {"receipt_refs": [receipt_ref]},
            **({"structure": page["structure"]} if "structure" in page else {}),
        }

    async def search_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search one source and return exact context ranges without creating evidence."""

        requested_source_ref = str(args.get("source_ref") or "").strip()
        query = str(args.get("query") or "").strip()
        if not requested_source_ref:
            return {"found": False, "error": "source_ref_required", "results": []}
        if len(query) < 3:
            return {
                "found": False,
                "error": "query must contain at least 3 non-whitespace characters",
                "results": [],
            }
        if len(query) > SOURCE_NAVIGATION_QUERY_MAX_CHARS:
            return {
                "found": False,
                "error": "query_too_long",
                "maximum": SOURCE_NAVIGATION_QUERY_MAX_CHARS,
                "received": len(query),
                "next_action": "Use one focused search query; split independent concepts into separate searches.",
                "results": [],
            }
        max_results, result_count_error = _validated_int(
            args.get("max_results"),
            field="max_results",
            minimum=1,
            maximum=SOURCE_NAVIGATION_SEARCH_MAX_RESULTS,
        )
        if result_count_error:
            return {"found": False, **result_count_error, "results": []}
        max_chars, size_error = _validated_int(
            args.get("max_chars"),
            field="max_chars",
            minimum=SOURCE_NAVIGATION_SEARCH_MIN_CHARS,
            maximum=SOURCE_NAVIGATION_SEARCH_MAX_CHARS,
        )
        if size_error:
            return {"found": False, **size_error, "results": []}
        guard_error = await target_aware_triage_read_guard(
            self,
            target_source_refs=[requested_source_ref],
            exact_read=False,
        )
        if guard_error:
            return {**guard_error, "results": []}
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=requested_source_ref,
            version_ref="",
        )
        if ref_error:
            return {"found": False, **ref_error, "results": []}
        version = await self._resolve_source_version(
            source_id="",
            source_version_id="",
            source_ref=requested_source_ref,
            version_ref="",
        )
        if version is None:
            return {
                "found": False,
                "error": "No readable source version in this run corpus view.",
                "results": [],
            }
        source_ref = await self._source_ref_for_id(version.source_id)
        ranges = await self._search_source_text_ranges(
            version,
            query=query,
            max_results=max_results,
            max_chars=max_chars,
        )
        if not ranges:
            load_error = _owner_source_version_text_error_payload(self, version)
            if load_error:
                return {
                    "found": False,
                    "source_ref": source_ref,
                    "results": [],
                    **load_error,
                }
        receipts = await self._persist_source_read_receipts(
            version=version,
            source_ref=source_ref,
            ranges=ranges,
            read_kind="search",
            request_metadata={
                "query": query,
                "max_results": max_results,
                "max_chars": max_chars,
            },
        )
        if "error" in receipts:
            return {"found": False, **receipts, "results": []}
        receipt_refs = receipts["receipt_refs"]
        source_text = _owner_source_version_text(self, version)
        results = []
        for index, item in enumerate(ranges):
            receipt_ref = receipt_refs[index]
            passages = receipts["passages_by_receipt_ref"][receipt_ref]
            match_text = "".join(str(passage.get("text") or "") for passage in passages)
            context = expanded_search_context(
                source_version_id=version.id,
                text=source_text,
                match_start=int(item["start_offset"]),
                match_end=int(item["end_offset"]),
                max_chars=SOURCE_NAVIGATION_SEARCH_MAX_CHARS,
            )
            model_context_ref = register_model_window_ref(
                self,
                source_ref=source_ref,
                canonical_ref=str(context["window_ref"]),
            )
            results.append(
                {
                    "match_text": match_text,
                    "range": _model_range(item),
                    "context_range": {
                        "start_offset": int(context["start_offset"]),
                        "end_offset": int(context["end_offset"]),
                        "offset_unit": "unicode_codepoint",
                        "end_exclusive": True,
                    },
                    "returned_chars": len(match_text),
                    "window_ref": model_context_ref,
                    **_model_navigation_locator(item["locator"]),
                    "locator_type": item["locator_type"],
                    "boundary_kind": item["boundary_kind"],
                    "receipt_ref": receipt_ref,
                    "navigation_only": True,
                    "exact_read_call": {
                        "tool": "read_source_window",
                        "arguments": {
                            "source_ref": source_ref,
                            "window_ref": model_context_ref,
                            "locator": "",
                            "max_chars": SOURCE_NAVIGATION_SEARCH_MAX_CHARS,
                        },
                    },
                }
            )
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.CORPUS_EVIDENCE,
            agent_name=V2AgentName.CORPUS_EVIDENCE_API,
            tool_name=V2ToolName.SEARCH_SOURCE,
            input_json={
                "source_ref": source_ref,
                "query": query,
                "max_results": max_results,
                "max_chars": max_chars,
            },
            output_json={
                "receipt_refs": receipt_refs,
                "ranges": [item["range"] for item in results],
                "result_count": len(results),
            },
        )
        result = {
            "found": bool(results),
            "source_ref": source_ref,
            "query": query,
            "result_count": len(results),
            "total_chars": ranges[0]["total_chars"] if ranges else len(_owner_source_version_text(self, version)),
            "results": results,
            "creates_evidence": False,
        }
        if receipt_refs:
            result["delivery_status"] = READ_RECEIPT_DELIVERY_DELIVERED
            result["_runtime_receipt_delivery"] = {"receipt_refs": receipt_refs}
        return result

    async def _triage_exact_duplicate_predecessor(
        self,
        source_ref: str,
    ) -> dict[str, Any] | None:
        set_id = str(getattr(self, "triage_current_set_id", None) or "")
        if not set_id:
            return None
        workspace = await load_source_triage_workspace(self.db, self.run.id)
        source_set = next(
            (
                item
                for item in (workspace or {}).get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
            ),
            None,
        )
        if source_set is None:
            return None
        context_loader = getattr(self, "_source_ref_context", None)
        if not callable(context_loader):
            return None
        return await exact_duplicate_predecessor(
            self.db,
            ref_context=await context_loader(),
            ordered_source_refs=[str(value) for value in source_set.get("source_refs") or []],
            source_ref=source_ref,
        )

    async def _persist_source_read_receipts(
        self,
        *,
        version: Any,
        source_ref: str,
        ranges: list[dict[str, Any]],
        read_kind: str,
        request_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if not ranges:
            return {"receipt_refs": [], "passages_by_receipt_ref": {}}
        workspace = await load_source_triage_workspace(self.db, self.run.id)
        set_id = str(
            getattr(self, "triage_current_set_id", None)
            or ((workspace or {}).get("current_set") or {}).get("set_id")
            or ""
        )
        if workspace is None or not set_id:
            return {
                "error": "triage_workspace_receipt_unavailable",
                "message": "Source text was not returned because its durable read receipt could not be saved.",
            }
        source_set = next(
            (
                item
                for item in workspace.get("source_sets") or []
                if isinstance(item, dict) and str(item.get("set_id") or "") == set_id
            ),
            None,
        )
        if source_set is None or source_ref not in {str(item) for item in source_set.get("source_refs") or []}:
            return {
                "error": "source_outside_current_triage_set",
                "source_ref": source_ref,
                "set_id": set_id,
            }
        text = _owner_source_version_text(self, version)
        if not text:
            load_error = _owner_source_version_text_error_payload(self, version)
            if load_error:
                return load_error
            return {"error": "source_has_no_readable_text"}
        content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = {
            str(item.get("receipt_ref") or ""): item
            for item in workspace.get("source_read_receipts") or []
            if isinstance(item, dict) and str(item.get("receipt_ref") or "")
        }
        receipt_refs: list[str] = []
        passages_by_receipt_ref: dict[str, list[dict[str, Any]]] = {}
        allow_timestamps = await self._source_allows_timestamp_locators(version)
        for item in ranges:
            start_offset = int(item["start_offset"])
            end_offset = int(item["end_offset"])
            receipt_ref = _source_read_receipt_ref(
                set_id=set_id,
                source_ref=source_ref,
                source_version_id=str(version.id),
                content_sha256=content_sha256,
                start_offset=start_offset,
                end_offset=end_offset,
                read_kind=read_kind,
            )
            receipt_refs.append(receipt_ref)
            passages = source_passages_for_range(
                source_version_id=version.id,
                text=text,
                start_offset=start_offset,
                end_offset=end_offset,
                allow_timestamps=allow_timestamps,
            )
            stored_receipt = existing.setdefault(
                receipt_ref,
                {
                    "receipt_ref": receipt_ref,
                    "set_id": set_id,
                    "source_ref": source_ref,
                    "source_version_id": str(version.id),
                    "content_sha256": content_sha256,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                    "total_chars": len(text),
                    "read_kind": read_kind,
                    "window_ref": (str(item.get("window_ref") or "") if read_kind == "search" else ""),
                    "next_cursor": (receipt_ref if read_kind == "sequential" and not bool(item.get("eof")) else ""),
                    "eof": bool(item.get("eof")),
                    "request": dict(request_metadata),
                    "passage_refs": [str(passage["passage_ref"]) for passage in passages],
                    **({"structure": item["structure"]} if isinstance(item.get("structure"), dict) else {}),
                    "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
                    "created_at": workspace_now(),
                },
            )
            stored_receipt["passage_refs"] = [
                str(passage["passage_ref"]) for passage in passages
            ]
            # Canonical signed refs remain backend-only. The model receives
            # short handles that are durably mapped inside the receipt.
            passages_by_receipt_ref[receipt_ref] = attach_source_passage_handles(
                {**workspace, "source_read_receipts": list(existing.values())},
                receipt=stored_receipt,
                passages=passages,
            )
        workspace["source_read_receipts"] = list(existing.values())
        await save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=workspace,
        )
        return {
            "receipt_refs": receipt_refs,
            "passages_by_receipt_ref": passages_by_receipt_ref,
        }

    async def materialize_source_passages(
        self,
        *,
        source_ref: str,
        passage_refs: list[str],
        allowed_read_kinds: set[str] | None = None,
    ) -> dict[str, Any]:
        """Turn delivered passages into canonical EvidenceItems without another model read."""

        version = await self._resolve_source_version(
            source_id="",
            source_version_id="",
            source_ref=source_ref,
            version_ref="",
        )
        if version is None:
            return {"accepted": False, "error": "source_version_unavailable_for_passage"}
        canonical_source_ref = await self._source_ref_for_id(version.source_id)
        if canonical_source_ref != source_ref:
            return {"accepted": False, "error": "passage_source_ref_mismatch"}
        text = _owner_source_version_text(self, version)
        workspace = await load_source_triage_workspace(self.db, self.run.id)
        set_id = str(getattr(self, "triage_current_set_id", None) or "")
        delivered_refs, ref_error = validate_delivered_passage_refs(
            workspace or {},
            set_id=set_id,
            source_ref=source_ref,
            passage_refs=passage_refs,
            allowed_read_kinds=allowed_read_kinds,
        )
        requested = list(dict.fromkeys(str(value) for value in passage_refs if str(value).strip()))
        if ref_error:
            return ref_error
        resolved_passages: list[tuple[str, str, int, int]] = []
        for model_passage_ref in requested:
            canonical_passage_ref = delivered_refs[model_passage_ref]
            try:
                resolved = resolve_source_passage_ref(
                    canonical_passage_ref,
                    source_version_id=version.id,
                    text=text,
                )
            except ValueError as exc:
                return {
                    "accepted": False,
                    "error": str(exc),
                    "source_ref": source_ref,
                    "invalid_passage_refs": [model_passage_ref],
                    **passage_ref_repair_payload(
                        [model_passage_ref],
                        available_ref_map=delivered_refs,
                    ),
                }
            resolved_passages.append(
                (
                    model_passage_ref,
                    canonical_passage_ref,
                    int(resolved["start_offset"]),
                    int(resolved["end_offset"]),
                )
            )
        evidence_ids: list[str] = []
        for model_passage_ref, canonical_passage_ref, start_offset, end_offset in resolved_passages:
            locator = await self._read_source_window(
                version,
                locator=canonical_passage_ref,
                max_chars=end_offset - start_offset,
            )
            snippet, locator_text, locator_type, channel = locator
            evidence = await persist_evidence_item(
                self.db,
                generation_run_id=self.run.id,
                project_id=self.run.project_id,
                source_id=version.source_id,
                source_version_id=version.id,
                provider=None,
                retrieval_channel=channel,
                purpose="triage_source_finding",
                search_result_index=0,
                score=1.0,
                snippet_text=snippet,
                normalized_quote_text=_normalize(snippet),
                locator_text=locator_text,
                locator_type=locator_type,
                evidence_strength=EvidenceStrength.EXACT,
                metadata={
                    "source_passage": True,
                    "passage_ref": canonical_passage_ref,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                },
            )
            await self._mark_evidence_exact_read(evidence, method="source_passage_materialization")
            evidence_ids.append(str(evidence.id))
        return {"accepted": True, "evidence_ids": evidence_ids}

    async def _resolve_source_read_cursor(
        self,
        *,
        version: Any,
        source_ref: str,
        cursor: str,
        max_chars: int,
    ) -> dict[str, Any]:
        if not cursor:
            return {"canonical_cursor": ""}
        set_id = str(getattr(self, "triage_current_set_id", None) or "")
        workspace = (
            await load_source_triage_workspace(self.db, self.run.id)
            if set_id
            else None
        )
        receipt = next(
            (
                item
                for item in (workspace or {}).get("source_read_receipts") or []
                if isinstance(item, dict) and str(item.get("receipt_ref") or "") == cursor
            ),
            None,
        )
        if receipt is None:
            return {
                "error": "invalid_or_stale_source_cursor",
                "next_action": f"Retry read_source with source_ref='{source_ref}' and cursor=''.",
            }
        if not source_read_receipt_is_delivered(receipt):
            delivered_receipts = [
                item
                for item in (workspace or {}).get("source_read_receipts") or []
                if isinstance(item, dict)
                and str(item.get("set_id") or "") == set_id
                and str(item.get("source_ref") or "") == source_ref
                and str(item.get("read_kind") or "") == "sequential"
                and source_read_receipt_is_delivered(item)
            ]
            retry_cursor = (
                str(delivered_receipts[-1].get("receipt_ref") or "")
                if delivered_receipts
                else ""
            )
            return {
                "error": "source_cursor_not_delivered",
                "receipt_ref": cursor,
                "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
                "next_action": (
                    f"Retry read_source with source_ref='{source_ref}' and "
                    f"cursor='{retry_cursor}'. The pending range was not delivered."
                ),
            }
        text = _owner_source_version_text(self, version)
        if not text:
            load_error = _owner_source_version_text_error_payload(self, version)
            if load_error:
                return load_error
            return {"error": "source_has_no_readable_text"}
        content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if (
            str(receipt.get("set_id") or "") != set_id
            or str(receipt.get("source_ref") or "") != source_ref
            or str(receipt.get("source_version_id") or "") != str(version.id)
            or str(receipt.get("content_sha256") or "") != content_sha256
        ):
            return {
                "error": "source_cursor_identity_mismatch",
                "next_action": f"Restart this source with source_ref='{source_ref}' and cursor=''.",
            }
        start_offset = int(receipt.get("end_offset") or 0)
        if bool(receipt.get("eof")) or start_offset >= len(text):
            return {
                "error": "source_cursor_already_at_eof",
                "source_ref": source_ref,
                "eof": True,
            }
        return {
            "canonical_cursor": create_window_ref(
                source_version_id=version.id,
                text=text,
                start_offset=start_offset,
                end_offset=min(len(text), start_offset + max_chars),
            )
        }

    async def inspect_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        requested_source_ref = str(args.get("source_ref") or "").strip()
        guard_error = await target_aware_triage_read_guard(
            self,
            target_source_refs=[requested_source_ref] if requested_source_ref else [],
            exact_read=False,
        )
        if guard_error:
            return guard_error
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=requested_source_ref,
            version_ref=str(args.get("version_ref") or ""),
        )
        if ref_error:
            return {"found": False, **ref_error}
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=requested_source_ref,
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        source_ref = await self._source_ref_for_id(version.source_id)
        text = _owner_source_version_text(self, version)
        if not text:
            load_error = _owner_source_version_text_error_payload(self, version)
            return {"found": False, "source_ref": source_ref, **(load_error or {"error": "source_has_no_readable_text"})}
        set_id = str(getattr(self, "triage_current_set_id", None) or "")
        workspace = (
            await load_source_triage_workspace(self.db, self.run.id)
            if set_id
            else None
        )
        if set_id and workspace is None:
            return {
                "found": False,
                "accepted": False,
                "error": "triage_workspace_receipt_unavailable",
                "source_ref": source_ref,
                "message": "The source structure was not returned because it could not be recorded durably.",
                "next_action": "Retry inspect_source after the triage workspace is available.",
            }
        content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cursor = str(args.get("cursor") or "").strip()
        placeholder_receipt_ref = (
            f"structure_receipt:{'0' * 24}" if workspace is not None and set_id else ""
        )
        payload = {
            "source_ref": source_ref,
            "kind": version.kind,
            **build_source_structure_page(
                text=text,
                source_version_id=version.id,
                allow_timestamps=await self._source_allows_timestamp_locators(version),
                cursor=cursor,
                max_items=max(1, min(int(args.get("max_items") or 24), 24)),
                delivery_receipt_ref=placeholder_receipt_ref,
            ),
        }
        if workspace is None or not set_id or payload.get("found") is not True:
            return payload
        receipt_ref = source_structure_receipt_ref(
            set_id=set_id,
            source_ref=source_ref,
            source_version_id=str(version.id),
            content_sha256=content_sha256,
            cursor=str(payload.get("cursor") or "0"),
            returned_count=int(payload.get("returned_count") or 0),
        )
        payload["structure_receipt_ref"] = receipt_ref
        stage_source_structure_page(
            workspace,
            set_id=set_id,
            source_ref=source_ref,
            source_version_id=str(version.id),
            content_sha256=content_sha256,
            payload=payload,
            receipt_ref=receipt_ref,
        )
        await save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace=workspace,
        )
        payload["_runtime_receipt_delivery"] = {"receipt_refs": [receipt_ref]}
        return payload

    async def read_source_window_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        requested_source_ref = str(args.get("source_ref") or "").strip()
        locator = str(args.get("locator") or "").strip()
        model_window_ref = str(args.get("window_ref") or "").strip()
        receipt_ref = str(args.get("receipt_ref") or "").strip()
        exact_quote = str(args.get("quote") or "")
        has_start_offset = args.get("start_offset") not in {None, ""}
        has_end_offset = args.get("end_offset") not in {None, ""}
        if receipt_ref and (model_window_ref or locator):
            return {
                "found": False,
                "error": "read_source_window_selector_conflict",
                "message": (
                    "Pass exactly one selector: receipt_ref, window_ref, or locator. "
                    "A receipt subrange or quote belongs only to receipt_ref."
                ),
            }
        if not receipt_ref and (exact_quote or has_start_offset or has_end_offset):
            return {
                "found": False,
                "error": "receipt_ref_required_for_receipt_selection",
                "message": "quote/start_offset/end_offset may be used only with receipt_ref.",
            }
        if receipt_ref and exact_quote and (has_start_offset or has_end_offset):
            return {
                "found": False,
                "error": "receipt_subrange_selector_conflict",
                "message": "Choose either quote or start_offset/end_offset inside the receipt, not both.",
            }
        if receipt_ref and has_start_offset != has_end_offset:
            return {
                "found": False,
                "error": "receipt_subrange_incomplete",
                "message": "Pass both start_offset and end_offset, or neither.",
            }
        if model_window_ref and locator:
            return {
                "found": False,
                "error": "read_source_window_selector_conflict",
                "message": "Pass exactly one selector: window_ref with locator='', or locator with window_ref=''.",
            }
        if not model_window_ref and (is_window_ref(locator) or resolve_model_window_ref(self, locator)):
            model_window_ref = locator
            locator = ""
        window_ref = resolve_model_window_ref(self, model_window_ref) if model_window_ref else ""
        if model_window_ref:
            locator = ""
        parsed_window_ref = None
        if model_window_ref:
            if not window_ref:
                recovery = await self._source_window_recovery_payload(source_ref=requested_source_ref)
                return {
                    "found": False,
                    "error": "invalid_window_ref",
                    "message": "Use a returned stable window_ref; the source remains readable when a handle is invalid.",
                    **recovery,
                }
            try:
                parsed_window_ref = parse_window_ref(window_ref)
            except WindowRefError:
                recovery = await self._source_window_recovery_payload(source_ref=requested_source_ref)
                return {"found": False, "error": "invalid_window_ref", **recovery}
        receipt: dict[str, Any] | None = None
        if receipt_ref:
            receipt_result = await self._source_read_receipt_for_evidence(
                receipt_ref=receipt_ref,
                requested_source_ref=requested_source_ref,
            )
            if "error" in receipt_result:
                return {"found": False, **receipt_result}
            receipt = receipt_result["receipt"]
        elif (
            window_ref
            and requested_source_ref
            and getattr(self, "triage_current_set_id", None)
        ):
            receipt = await self._delivered_search_receipt_for_window(
                window_ref=window_ref,
                requested_source_ref=requested_source_ref,
            )
            if receipt is not None:
                receipt_ref = str(receipt.get("receipt_ref") or "")
        requested_version_ref = (
            ""
            if parsed_window_ref is not None or receipt is not None
            else str(args.get("version_ref") or "")
        )
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=str(args.get("source_ref") or ""),
            version_ref=requested_version_ref,
        )
        if ref_error:
            return {"found": False, **ref_error}
        if not receipt_ref and not model_window_ref and not locator:
            return {"found": False, "error": "window_ref_or_locator_required"}
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=(
                str(parsed_window_ref.source_version_id)
                if parsed_window_ref is not None
                else (
                    str(receipt.get("source_version_id") or "")
                    if receipt is not None
                    else str(args.get("source_version_id") or "")
                )
            ),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=requested_version_ref,
        )
        if version is None:
            if receipt_ref:
                return {
                    "found": False,
                    "error": "source_read_receipt_version_unavailable",
                    "receipt_ref": receipt_ref,
                    "message": "The receipt no longer resolves to a readable version in this run.",
                }
            if model_window_ref:
                recovery = await self._source_window_recovery_payload(
                    source_ref=requested_source_ref,
                )
                return {
                    "found": False,
                    "error": "window_ref_invalid_or_stale",
                    "message": "The source remains readable; use the returned current window handle.",
                    **recovery,
                }
            return {"found": False, "error": "No readable source version in this run corpus view."}
        canonical_source_ref = await self._source_ref_for_id(version.source_id)
        if requested_source_ref:
            resolved_source_id = await self._source_id_from_ref_or_id(requested_source_ref)
            if resolved_source_id and str(version.source_id) != resolved_source_id:
                return {
                    "found": False,
                    "error": "window_ref_source_mismatch" if model_window_ref else "source_version_mismatch",
                }
        # Materializing evidence from a delivered receipt reveals no new source
        # text. The original read retains its own checkpoint obligation, but
        # this extraction must not create a false source switch.
        guard_error = (
            None
            if receipt is not None
            else await target_aware_triage_read_guard(
                self,
                target_source_refs=[canonical_source_ref],
                exact_read=True,
            )
        )
        if guard_error:
            return guard_error
        max_chars = max(200, min(int(args.get("max_chars") or 2200), 3000))
        selected_receipt_range: dict[str, int] | None = None
        if receipt is not None:
            receipt_window = self._source_read_receipt_window(
                receipt=receipt,
                version=version,
                source_ref=canonical_source_ref,
                quote=exact_quote,
                start_offset=args.get("start_offset"),
                end_offset=args.get("end_offset"),
                max_chars=max_chars,
            )
            if "error" in receipt_window:
                return {"found": False, "receipt_ref": receipt_ref, **receipt_window}
            requested_locator = str(receipt_window["window_ref"])
            selected_receipt_range = {
                "start_offset": int(receipt_window["start_offset"]),
                "end_offset": int(receipt_window["end_offset"]),
            }
        else:
            requested_locator = window_ref or locator
        snippet, locator_text, locator_type, channel = await self._read_source_window(
            version,
            locator=requested_locator,
            max_chars=max_chars,
        )
        if not snippet.strip():
            load_error = _owner_source_version_text_error_payload(self, version)
            if load_error:
                return {"found": False, "source_ref": canonical_source_ref, **load_error}
            if requested_locator:
                recovery = await self._source_window_recovery_payload(
                    source_ref=requested_source_ref,
                    version=version,
                )
                return {
                    "found": False,
                    "error": "window_ref_invalid_or_stale" if model_window_ref else "locator_not_found",
                    "requested_locator": locator or None,
                    **recovery,
                }
            return {"found": False, "error": "Source version has no readable local text window."}
        exact_window = bool(receipt_ref or model_window_ref) or (
            bool(locator)
            and locator_type in {EvidenceLocatorType.PAGE, EvidenceLocatorType.TIMESTAMP, EvidenceLocatorType.SECTION}
        )
        evidence = await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider=None,
            retrieval_channel=channel,
            query_text=locator or None,
            purpose="source_read",
            search_result_index=0,
            score=1.0,
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet),
            locator_text=locator_text,
            locator_type=locator_type,
            evidence_strength=EvidenceStrength.EXACT if exact_window else EvidenceStrength.SNIPPET,
            metadata={
                "source_read": True,
                "requested_locator": locator,
                **({"window_ref": window_ref} if window_ref else {}),
                **({"receipt_ref": receipt_ref} if receipt_ref else {}),
                **({"receipt_range": selected_receipt_range} if selected_receipt_range else {}),
            },
        )
        await self._mark_evidence_exact_read(evidence, method="read_source_window")
        payload = await self._evidence_detail_payload(evidence, max_snippet_chars=max_chars)
        if getattr(self, "triage_finalizer_mode", False):
            from app.generation_v2.source_triage_finalizer_evidence import (
                record_finalizer_evidence_read,
            )

            await record_finalizer_evidence_read(
                self.db,
                generation_run_id=self.run.id,
                evidence_id=str(payload["evidence_id"]),
                exact_read_complete=True,
            )
        exact_read_control = (
            None
            if receipt is not None
            else await self._record_triage_exact_read(
                source_ref=canonical_source_ref,
                evidence_ids=[str(payload["evidence_id"])],
            )
        )
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.CORPUS_EVIDENCE,
            agent_name=V2AgentName.CORPUS_EVIDENCE_API,
            tool_name=V2ToolName.READ_SOURCE_WINDOW,
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "locator": locator,
                "window_ref": window_ref,
                "receipt_ref": receipt_ref,
                "receipt_range": selected_receipt_range,
                "max_chars": max_chars,
            },
            output_json={"evidence_id": payload["evidence_id"], "locator_text": payload["locator_text"]},
        )
        result = {
            "found": True,
            "evidence": payload,
            **({"window_ref": model_window_ref} if model_window_ref else {}),
            **({"receipt_ref": receipt_ref, "range": selected_receipt_range} if receipt_ref else {}),
        }
        if exact_read_control:
            result["exact_read_control"] = exact_read_control
        return result

    async def _source_read_receipt_for_evidence(
        self,
        *,
        receipt_ref: str,
        requested_source_ref: str,
    ) -> dict[str, Any]:
        workspace = await load_source_triage_workspace(self.db, self.run.id)
        set_id = str(
            getattr(self, "triage_current_set_id", None)
            or ((workspace or {}).get("current_set") or {}).get("set_id")
            or ""
        )
        receipt = next(
            (
                item
                for item in (workspace or {}).get("source_read_receipts") or []
                if isinstance(item, dict) and str(item.get("receipt_ref") or "") == receipt_ref
            ),
            None,
        )
        if receipt is None:
            return {
                "error": "invalid_or_stale_source_read_receipt",
                "receipt_ref": receipt_ref,
                "next_action": "Use a receipt_ref returned by read_source or search_source in the current set.",
            }
        if (
            not set_id
            or str(receipt.get("set_id") or "") != set_id
            or str(receipt.get("source_ref") or "") != requested_source_ref
        ):
            return {
                "error": "source_read_receipt_scope_mismatch",
                "receipt_ref": receipt_ref,
                "source_ref": requested_source_ref,
                "set_id": set_id,
            }
        if str(receipt.get("read_kind") or "") == "native_file_attachment":
            return {
                "error": "source_read_receipt_has_no_text_range",
                "receipt_ref": receipt_ref,
                "next_action": "Use a read_source or search_source receipt for local exact evidence.",
            }
        if not source_read_receipt_is_delivered(receipt):
            return {
                "error": "source_read_receipt_not_delivered",
                "receipt_ref": receipt_ref,
                "delivery_status": READ_RECEIPT_DELIVERY_PENDING,
                "next_action": (
                    "Repeat read_source or search_source for this range. The prior provider call did not "
                    "acknowledge its text."
                ),
            }
        return {"receipt": receipt}

    async def _delivered_search_receipt_for_window(
        self,
        *,
        window_ref: str,
        requested_source_ref: str,
    ) -> dict[str, Any] | None:
        """Reuse a delivered search range instead of treating evidence selection as a new read."""

        workspace = await load_source_triage_workspace(self.db, self.run.id)
        set_id = str(
            getattr(self, "triage_current_set_id", None)
            or ((workspace or {}).get("current_set") or {}).get("set_id")
            or ""
        )
        return next(
            (
                item
                for item in (workspace or {}).get("source_read_receipts") or []
                if isinstance(item, dict)
                and str(item.get("set_id") or "") == set_id
                and str(item.get("source_ref") or "") == requested_source_ref
                and str(item.get("read_kind") or "") == "search"
                and str(item.get("window_ref") or "") == window_ref
                and source_read_receipt_is_delivered(item)
            ),
            None,
        )

    def _source_read_receipt_window(
        self,
        *,
        receipt: dict[str, Any],
        version: Any,
        source_ref: str,
        quote: str,
        start_offset: Any,
        end_offset: Any,
        max_chars: int,
    ) -> dict[str, Any]:
        text = _owner_source_version_text(self, version)
        if not text:
            load_error = _owner_source_version_text_error_payload(self, version)
            return load_error or {"error": "source_has_no_readable_text"}
        return select_receipt_evidence_range(
            text=text,
            receipt=receipt,
            source_ref=source_ref,
            source_version_id=version.id,
            quote=quote,
            start_offset=start_offset,
            end_offset=end_offset,
            max_chars=max_chars,
        )

    async def search_within_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        requested_source_ref = str(args.get("source_ref") or "").strip()
        guard_error = await target_aware_triage_read_guard(
            self,
            target_source_refs=[requested_source_ref] if requested_source_ref else [],
            exact_read=False,
        )
        if guard_error:
            return {**guard_error, "results": []}
        ref_error = await self._invalid_source_or_version_ref_error(
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if ref_error:
            return {**ref_error, "results": []}
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"results": [], "error": "No readable source version in this run corpus view."}
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 6), 8))
        text = _owner_source_version_text(self, version)
        if text.strip():
            return await self._search_text_source_windows(version, query=query, text=text, limit=limit)
        load_error = _owner_source_version_text_error_payload(self, version)
        if load_error:
            return {"results": [], **load_error}
        return {"results": [], "error": "source_has_no_readable_text"}

    async def _search_text_source_windows(self, version: Any, *, query: str, text: str, limit: int) -> dict[str, Any]:
        return await _search_text_source_windows_action(self, version, query=query, text=text, limit=limit)

    async def skim_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await _skim_source_action(self, args)

    async def _source_window_recovery_payload(
        self,
        *,
        source_ref: str,
        version: Any | None = None,
    ) -> dict[str, Any]:
        resolved_version = version
        if resolved_version is None and source_ref:
            resolved_version = await self._resolve_source_version(
                source_id="",
                source_version_id="",
                source_ref=source_ref,
                version_ref="",
            )
        if resolved_version is None:
            return {
                "next_action": "Call inspect_source with a valid source_ref and cursor='' to obtain working windows.",
            }
        canonical_source_ref = str(source_ref or "").strip() or await self._source_ref_for_id(
            resolved_version.source_id
        )
        try:
            page = await self._sequential_source_window_page(
                resolved_version,
                cursor="",
                max_windows=1,
            )
        except ValueError:
            page = {"windows": []}
        windows = page.get("windows") or []
        if not windows:
            load_error = _owner_source_version_text_error_payload(self, resolved_version)
            if load_error:
                return {
                    **load_error,
                    "next_action": (
                        f"Retry the source read for source_ref='{canonical_source_ref}'. Do not treat a storage "
                        "failure as an empty source."
                    ),
                }
            return {
                "error": "source_has_no_readable_text",
                "next_action": f"Use another readable representation for source_ref='{canonical_source_ref}'.",
            }
        available = register_model_window_ref(
            self,
            source_ref=canonical_source_ref,
            canonical_ref=windows[0]["window_ref"],
        )
        return {
            "available_window_ref": available,
            "next_action": (
                f"Retry read_source_window with source_ref='{canonical_source_ref}', "
                f"window_ref='{available}', locator='', or call inspect_source with cursor=''."
            ),
        }


__all__ = ["EvidenceSourceToolsMixin"]
