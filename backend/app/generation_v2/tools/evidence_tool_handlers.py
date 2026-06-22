from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.source_triage_workspace import mark_workspace_write_action, triage_workflow_guard_error
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
    SourceVersion,
)
from app.generation_v2.tools.evidence_source_map import _relationship_kind, _source_summaries, build_source_inventory
from app.generation_v2.tools.evidence_refs import (
    SourceRefContext,
    build_source_ref_context,
    build_source_ref_context_from_ids,
    resolve_source_ref,
    source_domain_for_id,
    source_ref_for_id,
    source_title_for_id,
    version_ref_for_index,
)
from app.generation_v2.tools.evidence_store import (
    compact_model_read_result,
    persist_evidence_item,
)
from app.generation_v2.tools.evidence_types import (
    EVIDENCE_STATUS_CITED,
    EVIDENCE_STATUS_OBSERVED,
    EVIDENCE_STATUS_SELECTED,
)
from app.generation_v2.tools.evidence_utils import (
    _confidence,
    _dedupe,
    _hosted_result_file_id,
    _hosted_result_text,
    _model_file_read_snippet,
    _model_read_evidence_strength,
    _model_read_locator_type,
    _normalize,
    _quote_text_matches_evidence,
    _readable_version_priority,
    _retrieval_channel_for_version,
    _safe_metadata,
    _search_text_windows,
    _source_outline,
    _source_version_text,
    _unsupported_model_read_selected_as_support,
    _uuid_list,
    _uuid_or_none,
)


class EvidenceToolHandlersMixin:
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
        allowed_refs = getattr(self, "triage_allowed_source_refs", None)
        if not allowed_refs:
            return None
        source_ids, _ = await self._source_ids_from_refs_or_ids(sorted(str(item) for item in allowed_refs))
        return set(source_ids)

    async def _triage_scope_error_for_refs(self, refs: list[str]) -> dict[str, Any] | None:
        allowed_refs = getattr(self, "triage_allowed_source_refs", None)
        if not allowed_refs:
            return None
        invalid = [str(ref) for ref in refs if str(ref) not in allowed_refs]
        if not invalid:
            return None
        return {
            "accepted": False,
            "error": "source_refs_outside_current_triage_set",
            "message": "This source-set agent can only use source_refs from its current source set.",
            "invalid_source_refs": invalid,
            "allowed_source_refs": sorted(allowed_refs),
        }

    async def _triage_scope_error_for_source_ids(self, source_ids: list[str]) -> dict[str, Any] | None:
        allowed_ids = await self._triage_allowed_source_ids()
        if not allowed_ids:
            return None
        invalid = [str(source_id) for source_id in source_ids if str(source_id) not in allowed_ids]
        if not invalid:
            return None
        return {
            "accepted": False,
            "error": "sources_outside_current_triage_set",
            "message": "This source-set agent can only use sources from its current source set.",
            "invalid_source_ids": invalid,
        }

    async def _triage_read_guard_error(self) -> dict[str, Any] | None:
        return await triage_workflow_guard_error(
            self.db,
            generation_run_id=self.run.id,
            current_set_id=getattr(self, "triage_current_set_id", None),
        )

    async def _source_ref_for_id(self, source_id: uuid.UUID | str) -> str:
        context = await self._source_ref_context()
        return source_ref_for_id(str(source_id), context)

    async def _version_ref_for_version(self, version: SourceVersion) -> str:
        source_ref = await self._source_ref_for_id(version.source_id)
        versions = await self._versions_for_source(str(version.source_id))
        canonical_version_ids = set(_active_filter_list(self.run_view, "canonical_source_version_ids"))
        for idx, candidate in enumerate(versions, start=1):
            if str(candidate.id) == str(version.id):
                return version_ref_for_index(
                    source_ref,
                    index=idx,
                    canonical=str(candidate.id) in canonical_version_ids,
                )
        return f"{source_ref}:v?"

    async def _source_version_id_from_ref(self, version_ref: str) -> str | None:
        text = str(version_ref or "").strip()
        if not text:
            return None
        if _uuid_or_none(text) and self._source_version_in_run_view(text):
            return text
        if ":" not in text:
            return None
        source_ref, version_part = text.split(":", 1)
        source_id = await self._source_id_from_ref_or_id(source_ref)
        if not source_id:
            return None
        versions = await self._versions_for_source(source_id)
        canonical_version_ids = set(_active_filter_list(self.run_view, "canonical_source_version_ids"))
        if version_part == "canonical":
            for version in versions:
                if str(version.id) in canonical_version_ids:
                    return str(version.id)
            return None
        if version_part.startswith("v") and version_part[1:].isdigit():
            idx = int(version_part[1:])
            if 1 <= idx <= len(versions):
                return str(versions[idx - 1].id)
        return None

    async def _versions_for_source(self, source_id: str) -> list[SourceVersion]:
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.source_id == uuid.UUID(source_id),
                SourceVersion.id.in_(_uuid_list([str(item) for item in self.run_view.selected_source_version_ids])),
            )
        )
        return sorted(list(result.scalars()), key=lambda version: _readable_version_priority(version.kind))

    async def _evidence_preview_payload(self, item: EvidenceItem, *, max_short_snippet_chars: int = 420) -> dict[str, Any]:
        context = await self._source_ref_context()
        snippet = " ".join(str(item.snippet_text or "").split())
        if len(snippet) > max_short_snippet_chars:
            snippet = snippet[:max_short_snippet_chars].rstrip()
        payload: dict[str, Any] = {
            "evidence_id": str(item.id),
            "source_ref": source_ref_for_id(str(item.source_id), context),
            "source_title": source_title_for_id(str(item.source_id), context)[:220],
            "source_domain": source_domain_for_id(str(item.source_id), context),
            "locator": item.locator_text,
            "short_snippet": snippet,
            "score": item.score,
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    async def _evidence_preview_payloads(self, items: list[EvidenceItem], *, max_short_snippet_chars: int = 420) -> list[dict[str, Any]]:
        return [await self._evidence_preview_payload(item, max_short_snippet_chars=max_short_snippet_chars) for item in items]

    async def _evidence_detail_payload(self, item: EvidenceItem, *, max_snippet_chars: int = 2400) -> dict[str, Any]:
        context = await self._source_ref_context()
        snippet = str(item.snippet_text or "")
        if len(snippet) > max_snippet_chars:
            return {
                "evidence_id": str(item.id),
                "source_ref": source_ref_for_id(str(item.source_id), context),
                "source_title": source_title_for_id(str(item.source_id), context)[:220],
                "source_domain": source_domain_for_id(str(item.source_id), context),
                "locator_text": item.locator_text,
                "locator_type": item.locator_type,
                "error": "evidence_too_large_use_source_window",
                "snippet_chars": len(snippet),
            }
        metadata = item.metadata_ or {}
        payload: dict[str, Any] = {
            "evidence_id": str(item.id),
            "source_ref": source_ref_for_id(str(item.source_id), context),
            "source_title": source_title_for_id(str(item.source_id), context)[:220],
            "source_domain": source_domain_for_id(str(item.source_id), context),
            "locator_text": item.locator_text,
            "locator_type": item.locator_type,
            "snippet_text": snippet,
            "query_text": item.query_text,
            "purpose": item.purpose,
        }
        if metadata.get("model_file_read"):
            payload.update(
                {
                    "support_assessment": metadata.get("support_level"),
                    "support_summary": metadata.get("support_summary"),
                    "uncertainty": metadata.get("uncertainty"),
                    "quote_text": metadata.get("quote_text"),
                }
            )
        warning = _coverage_warning(item)
        if warning:
            payload["coverage_warning"] = warning
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    async def list_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit") or 25), 25))
        cursor = str(args.get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0
        context = await self._source_ref_context()
        inventory = context.inventory
        selected = inventory.source_cards[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(inventory.source_cards) else ""
        canonical_kinds = await self._canonical_kinds_by_source()
        return {
            "sources": [
                _compact_source_card(
                    card,
                    canonical_kind=canonical_kinds.get(card.source_id),
                    source_ref=source_ref_for_id(card.source_id, context),
                )
                for card in selected
            ],
            "next_cursor": next_cursor,
            "total": len(inventory.source_cards),
        }

    async def search_corpus_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return {**guard_error, "results": []}
        query = str(args.get("query") or "").strip()
        if len(query) < 3:
            return {"accepted": False, "error": "query must contain at least 3 non-whitespace characters", "results": []}
        scope = str(args.get("scope") or "").strip() or ("selected_sources" if args.get("source_refs") else "all_sources")
        if scope not in {"all_sources", "selected_sources"}:
            return {"accepted": False, "error": "scope must be all_sources or selected_sources", "results": []}
        source_refs = [str(item) for item in args.get("source_refs") or [] if item]
        if getattr(self, "triage_allowed_source_refs", None) and scope == "all_sources":
            return {
                "accepted": False,
                "error": "all_sources_search_not_allowed_in_current_triage_set",
                "message": "Search only within source_refs from the current source set.",
                "allowed_source_refs": sorted(getattr(self, "triage_allowed_source_refs", set()) or []),
                "results": [],
            }
        scope_error = await self._triage_scope_error_for_refs(source_refs)
        if scope_error:
            return {**scope_error, "results": []}
        source_ids: list[str] = []
        if scope == "selected_sources":
            if not source_refs:
                return {"accepted": False, "error": "source_refs is required when scope=selected_sources", "results": []}
            source_ids, invalid = await self._source_ids_from_refs_or_ids(source_refs)
            if invalid:
                return {"accepted": False, "error": "source_refs include values outside this run corpus view", "invalid_source_refs": invalid, "results": []}
        limit = max(1, min(int(args.get("limit") or 5), 8))
        return await self.search_corpus(
            query=query,
            purpose=str(args.get("purpose") or "general"),
            source_ids=source_ids,
            limit=limit,
            scope=scope,
        )

    async def read_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        try:
            evidence_id = uuid.UUID(str(args.get("evidence_id") or ""))
        except ValueError:
            return {"found": False, "error": "Invalid evidence_id"}
        item = await self.db.get(EvidenceItem, evidence_id)
        if item is None or item.generation_run_id != self.run.id:
            return {"found": False}
        scope_error = await self._triage_scope_error_for_source_ids([str(item.source_id)])
        if scope_error:
            return scope_error
        payload = await self._evidence_detail_payload(item, max_snippet_chars=2400)
        if payload.get("error"):
            return {"found": False, **payload}
        return {"found": True, "evidence": payload}

    async def inspect_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        max_items = max(1, min(int(args.get("max_items") or 24), 40))
        text = _source_version_text(version)
        items = _source_outline(text, max_items=max_items) if text.strip() else []
        if not items:
            chunks = await self._chunks_for_source(str(version.source_id), limit=max_items)
            items = [
                {
                    "locator": " > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}",
                    "locator_type": "section" if chunk.heading_path else "chunk",
                    "preview": chunk.content[:180],
                }
                for chunk in chunks
            ]
        if not items:
            windows = await self._source_windows(version, max_windows=min(max_items, 12))
            items = [
                {
                    "locator": item["locator_text"],
                    "locator_type": item["locator_type"],
                    "preview": item["snippet_text"][:180],
                }
                for item in windows
            ]
        return {
            "found": bool(items),
            "source_ref": await self._source_ref_for_id(version.source_id),
            "version_ref": await self._version_ref_for_version(version),
            "kind": version.kind,
            "items": items,
        }

    async def read_source_window_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        locator = str(args.get("locator") or "")
        max_chars = max(200, min(int(args.get("max_chars") or 2200), 3000))
        snippet, locator_text, locator_type, channel = await self._read_source_window(version, locator=locator, max_chars=max_chars)
        if not snippet.strip():
            return {"found": False, "error": "Source version has no readable local text window."}
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
            normalized_quote_text=_normalize(snippet[:500]),
            locator_text=locator_text,
            locator_type=locator_type,
            evidence_strength=EvidenceStrength.EXACT if locator else EvidenceStrength.SNIPPET,
            metadata={"source_read": True, "requested_locator": locator},
        )
        payload = await self._evidence_detail_payload(evidence, max_snippet_chars=max_chars)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="read_source_window",
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "locator": locator,
                "max_chars": max_chars,
            },
            output_json={"evidence_id": payload["evidence_id"], "locator_text": payload["locator_text"]},
        )
        return {"found": True, "evidence": payload}

    async def read_source_with_model_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        focus_question = str(args.get("focus_question") or "").strip()
        claim_text = str(args.get("claim_text") or "").strip()
        purpose = str(args.get("purpose") or "model_file_read").strip() or "model_file_read"
        locator_hint = str(args.get("locator_hint") or "").strip()
        if not (focus_question or claim_text):
            return {"found": False, "error": "focus_question or claim_text is required"}

        source_id = str(args.get("source_id") or "")
        source_version_id = str(args.get("source_version_id") or "")
        source_ref = str(args.get("source_ref") or "")
        version_ref = str(args.get("version_ref") or "")
        if source_ref and not source_id:
            source_id = await self._source_id_from_ref_or_id(source_ref) or ""
        if version_ref and not source_version_id:
            source_version_id = await self._source_version_id_from_ref(version_ref) or ""
        request_cache_key = self._model_read_request_cache_key(
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
        )
        cached_request_items = await self._cached_evidence_items(cache_field="model_read_request_cache_key", cache_key=request_cache_key, limit=1)
        if cached_request_items:
            cached = cached_request_items[0]
            payload = await self._evidence_detail_payload(cached, max_snippet_chars=2400)
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="read_source_with_model",
                input_json={
                    "source_id": source_id,
                    "source_version_id": source_version_id,
                    "purpose": purpose,
                    "focus_question": focus_question,
                    "claim_text": claim_text,
                    "locator_hint": locator_hint,
                    "request_cache_key": request_cache_key,
                },
                output_json={"cache_hit": True, "evidence_id": payload["evidence_id"], "support_level": (cached.metadata_ or {}).get("support_level")},
                provider_call_id=cached.response_id,
            )
            response: dict[str, Any] = {
                "found": True,
                "cache_hit": True,
                "evidence": payload,
            }
            if cached.retrieval_channel == EvidenceRetrievalChannel.MODEL_FILE_READ_LOCAL_FALLBACK:
                response["coverage_warning"] = _coverage_warning(cached)
            else:
                response["model_read"] = _cached_model_read_payload(cached)
            return response
        provider_failures: list[dict[str, Any]] = []
        candidates: list[tuple[ProviderCorpusFile, SourceVersion]] = []
        if settings.openai_api_key:
            candidates = await self._resolve_provider_file_candidates_for_model_read(
                source_id=source_id,
                source_version_id=source_version_id,
            )

        for provider_file, version in candidates:
            cache_key = self._model_read_cache_key(
                source_version_id=str(version.id),
                focus_question=focus_question,
                claim_text=claim_text,
                purpose=purpose,
                locator_hint=locator_hint,
                read_surface_id=str(provider_file.provider_file_id or ""),
            )
            cached_items = await self._cached_evidence_items(cache_field="model_read_cache_key", cache_key=cache_key, limit=1)
            if cached_items:
                cached = cached_items[0]
                payload = await self._evidence_detail_payload(cached, max_snippet_chars=2400)
                await save_tool_trace(
                    self.db,
                    generation_run_id=self.run.id,
                    phase="v2_corpus_evidence",
                    agent_name="corpus_evidence_api",
                    tool_name="read_source_with_model",
                    input_json={
                        "source_id": str(version.source_id),
                        "source_version_id": str(version.id),
                        "purpose": purpose,
                        "focus_question": focus_question,
                        "claim_text": claim_text,
                        "locator_hint": locator_hint,
                        "cache_key": cache_key,
                    },
                    output_json={"cache_hit": True, "evidence_id": payload["evidence_id"], "support_level": (cached.metadata_ or {}).get("support_level")},
                    provider_call_id=cached.response_id,
                )
                return {
                    "found": True,
                    "cache_hit": True,
                    "evidence": payload,
                    "model_read": _cached_model_read_payload(cached),
                }
            try:
                read_result, response_id = await self._model_read_provider_file(
                    provider_file=provider_file,
                    version=version,
                    focus_question=focus_question,
                    claim_text=claim_text,
                    purpose=purpose,
                    locator_hint=locator_hint,
                )
            except Exception as exc:
                failure = {
                    "source_id": str(version.source_id),
                    "source_version_id": str(version.id),
                    "error": str(exc),
                }
                provider_failures.append(failure)
                await self._save_model_read_failure_trace(
                    provider_file=provider_file,
                    version=version,
                    purpose=purpose,
                    focus_question=focus_question,
                    claim_text=claim_text,
                    locator_hint=locator_hint,
                    error=str(exc),
                )
                continue

            snippet_text = _model_file_read_snippet(read_result)
            if not snippet_text.strip():
                provider_failures.append(
                    {
                        "source_id": str(version.source_id),
                        "source_version_id": str(version.id),
                        "error": "Model file read returned no quote, snippet, or support summary",
                    }
                )
                continue

            evidence = await self._persist_model_read_evidence(
                provider_file=provider_file,
                version=version,
                read_result=read_result,
                response_id=response_id,
                focus_question=focus_question,
                claim_text=claim_text,
                purpose=purpose,
                locator_hint=locator_hint,
                cache_key=cache_key,
                request_cache_key=request_cache_key,
            )
            payload = await self._evidence_detail_payload(evidence, max_snippet_chars=2400)
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="read_source_with_model",
                input_json={
                    "source_id": str(version.source_id),
                    "source_version_id": str(version.id),
                    "provider_file_id": provider_file.provider_file_id,
                    "purpose": purpose,
                    "focus_question": focus_question,
                    "claim_text": claim_text,
                    "locator_hint": locator_hint,
                    "candidate_count": len(candidates),
                    "prior_failure_count": len(provider_failures),
                    "cache_key": cache_key,
                    "request_cache_key": request_cache_key,
                },
                output_json={"cache_hit": False, "evidence_id": payload["evidence_id"], "support_level": read_result.get("support_level")},
                provider_call_id=response_id,
            )
            return {"found": True, "evidence": payload, "model_read": _model_read_agent_payload(read_result)}

        return await self._model_read_local_fallback(
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            request_cache_key=request_cache_key,
            provider_failures=provider_failures,
            provider_candidate_count=len(candidates),
        )

    async def _persist_model_read_evidence(
        self,
        *,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
        read_result: dict[str, Any],
        response_id: str | None,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
        cache_key: str,
        request_cache_key: str,
    ) -> EvidenceItem:
        snippet_text = _model_file_read_snippet(read_result)
        attributes = provider_file.attributes or {}
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider="openai",
            provider_vector_store_id=self.provider_index.provider_vector_store_id if self.provider_index else None,
            provider_file_id=str(read_result.get("_model_read_file_id") or provider_file.provider_file_id),
            provider_vector_store_file_id=provider_file.provider_vector_store_file_id,
            response_id=response_id,
            retrieval_channel=EvidenceRetrievalChannel.MODEL_FILE_READ,
            query_text=focus_question or claim_text,
            purpose=purpose,
            search_result_index=0,
            score=_confidence(read_result.get("confidence")),
            snippet_text=snippet_text,
            normalized_quote_text=_normalize(str(read_result.get("quote_text") or read_result.get("snippet_text") or snippet_text)[:500]),
            locator_text=str(read_result.get("locator_text") or attributes.get("filename") or "model file read"),
            locator_type=_model_read_locator_type(read_result),
            evidence_strength=_model_read_evidence_strength(read_result),
            metadata={
                "usage_status": EVIDENCE_STATUS_OBSERVED,
                "model_file_read": True,
                "support_summary": read_result.get("support_summary"),
                "uncertainty": read_result.get("uncertainty"),
                "supports_query": read_result.get("supports_query"),
                "support_level": read_result.get("support_level"),
                "quote_text": read_result.get("quote_text"),
                "locator_hint": locator_hint,
                "source_version_kind": version.kind,
                "model_read_file_id": read_result.get("_model_read_file_id"),
                "vector_store_provider_file_id": provider_file.provider_file_id,
                "provider_corpus_file_id": str(provider_file.id),
                "model_read_cache_key": cache_key,
                "model_read_request_cache_key": request_cache_key,
            },
        )

    async def _model_read_local_fallback(
        self,
        *,
        source_id: str,
        source_version_id: str,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
        request_cache_key: str,
        provider_failures: list[dict[str, Any]],
        provider_candidate_count: int,
    ) -> dict[str, Any]:
        version = await self._resolve_local_fallback_source_version(source_id=source_id, source_version_id=source_version_id)
        if version is None:
            return {
                "found": False,
                "error": "No provider-backed or locally readable source version is available in this run corpus view.",
                "provider_failure_count": len(provider_failures),
            }
        cache_key = self._model_read_cache_key(
            source_version_id=str(version.id),
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            read_surface_id="local_fallback",
        )
        cached_items = await self._cached_evidence_items(cache_field="model_read_cache_key", cache_key=cache_key, limit=1)
        if cached_items:
            cached = cached_items[0]
            payload = await self._evidence_detail_payload(cached, max_snippet_chars=2400)
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="read_source_with_model",
                input_json={
                    "source_id": str(version.source_id),
                    "source_version_id": str(version.id),
                    "purpose": purpose,
                    "focus_question": focus_question,
                    "claim_text": claim_text,
                    "locator_hint": locator_hint,
                    "cache_key": cache_key,
                    "request_cache_key": request_cache_key,
                },
                output_json={"cache_hit": True, "evidence_id": payload["evidence_id"], "local_fallback": True},
                provider_call_id=cached.response_id,
            )
            return {
                "found": True,
                "cache_hit": True,
                "evidence": payload,
                "coverage_warning": _coverage_warning(cached),
                "provider_failure_count": len(provider_failures),
            }
        source_id = source_id or str(version.source_id)
        locator = locator_hint or claim_text or focus_question
        snippet, locator_text, locator_type, channel = await self._read_source_window(version, locator=locator, max_chars=3000)
        if not snippet.strip():
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="read_source_with_model.local_fallback",
                input_json={
                    "source_id": str(version.source_id),
                    "source_version_id": str(version.id),
                    "purpose": purpose,
                    "focus_question": focus_question,
                    "claim_text": claim_text,
                    "locator_hint": locator_hint,
                    "provider_candidate_count": provider_candidate_count,
                },
                output_json={"found": False, "provider_failures": provider_failures},
            )
            return {
                "found": False,
                "error": "Provider model-read failed or was unavailable, and local fallback returned no readable window.",
                "provider_failure_count": len(provider_failures),
            }
        evidence = await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=version.source_id,
            source_version_id=version.id,
            provider=None,
            retrieval_channel=EvidenceRetrievalChannel.MODEL_FILE_READ_LOCAL_FALLBACK,
            query_text=focus_question or claim_text,
            purpose=purpose,
            search_result_index=0,
            score=0.65,
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet[:500]),
            locator_text=locator_text or "local source window",
            locator_type=locator_type,
            evidence_strength=EvidenceStrength.WEAK,
            metadata={
                "usage_status": EVIDENCE_STATUS_OBSERVED,
                "model_file_read": False,
                "model_file_read_local_fallback": True,
                "local_read_channel": channel,
                "source_version_kind": version.kind,
                "support_summary": "Local fallback returned a focused source window after provider model-read was unavailable or failed.",
                "locator_hint": locator_hint,
                "provider_candidate_count": provider_candidate_count,
                "provider_failures": provider_failures,
                "model_read_cache_key": cache_key,
                "model_read_request_cache_key": request_cache_key,
            },
        )
        payload = await self._evidence_detail_payload(evidence, max_snippet_chars=2400)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="read_source_with_model",
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "purpose": purpose,
                "focus_question": focus_question,
                "claim_text": claim_text,
                "locator_hint": locator_hint,
                "provider_candidate_count": provider_candidate_count,
                "cache_key": cache_key,
                "request_cache_key": request_cache_key,
            },
            output_json={
                "cache_hit": False,
                "evidence_id": payload["evidence_id"],
                "local_fallback": True,
                "local_read_channel": channel,
                "provider_failures": provider_failures,
            },
        )
        return {
            "found": True,
            "evidence": payload,
            "coverage_warning": _coverage_warning(evidence),
            "provider_failure_count": len(provider_failures),
        }

    async def search_within_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
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
        text = _source_version_text(version)
        if text.strip():
            windows = _search_text_windows(text, query=query, max_windows=limit, max_chars=1800)
            results = []
            for idx, window in enumerate(windows):
                evidence = await persist_evidence_item(
                    self.db,
                    generation_run_id=self.run.id,
                    project_id=self.run.project_id,
                    source_id=version.source_id,
                    source_version_id=version.id,
                    provider=None,
                    retrieval_channel=_retrieval_channel_for_version(version),
                    query_text=query,
                    purpose="source_internal_search",
                    search_result_index=idx,
                    score=window["score"],
                    snippet_text=window["snippet_text"],
                    normalized_quote_text=_normalize(window["snippet_text"][:500]),
                    locator_text=window["locator_text"],
                    locator_type=window["locator_type"],
                    evidence_strength=EvidenceStrength.EXACT if window["locator_type"] in {EvidenceLocatorType.PAGE, EvidenceLocatorType.TIMESTAMP, EvidenceLocatorType.SECTION} else EvidenceStrength.SNIPPET,
                    metadata={"source_internal_search": True},
                )
                results.append(await self._evidence_preview_payload(evidence, max_short_snippet_chars=420))
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="search_within_source",
                input_json={"source_id": str(version.source_id), "source_version_id": str(version.id), "query": query, "limit": limit},
                output_json={"evidence_ids": [item["evidence_id"] for item in results], "result_count": len(results)},
            )
            return {"results": results}
        return await self.search_corpus(query=query, purpose="source_internal_search", source_ids=[str(version.source_id)], limit=limit)

    async def skim_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return guard_error
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
            source_ref=str(args.get("source_ref") or ""),
            version_ref=str(args.get("version_ref") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        max_windows = max(1, min(int(args.get("max_windows") or 6), 8))
        windows = await self._source_windows(version, max_windows=max_windows)
        results = []
        for idx, window in enumerate(windows):
            evidence = await persist_evidence_item(
                self.db,
                generation_run_id=self.run.id,
                project_id=self.run.project_id,
                source_id=version.source_id,
                source_version_id=version.id,
                provider=None,
                retrieval_channel=window["channel"],
                query_text=window["locator_text"],
                purpose="source_skim",
                search_result_index=idx,
                score=float(max_windows - idx),
                snippet_text=window["snippet_text"],
                normalized_quote_text=_normalize(window["snippet_text"][:500]),
                locator_text=window["locator_text"],
                locator_type=window["locator_type"],
                evidence_strength=EvidenceStrength.SNIPPET,
                metadata={"source_skim": True},
            )
            window_payload = {key: value for key, value in window.items() if key != "snippet_text"}
            window_payload["preview_text"] = str(window.get("snippet_text") or "")[:240]
            window_payload["evidence"] = await self._evidence_preview_payload(evidence, max_short_snippet_chars=420)
            results.append(window_payload)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="skim_source",
            input_json={"source_id": str(version.source_id), "source_version_id": str(version.id), "max_windows": max_windows},
            output_json={"evidence_ids": [item["evidence"]["evidence_id"] for item in results], "window_count": len(results)},
        )
        return {
            "found": bool(results),
            "source_ref": await self._source_ref_for_id(version.source_id),
            "version_ref": await self._version_ref_for_version(version),
            "outline": [item["locator_text"] for item in results],
            "windows": results,
        }

    async def find_quotes_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_refs = [str(item) for item in args.get("source_refs") or [] if item]
        source_ids, invalid = await self._source_ids_from_refs_or_ids(source_refs)
        if invalid:
            return {"accepted": False, "error": "source_refs include values outside this run corpus view", "invalid_source_refs": invalid, "results": []}
        result = await self.search_corpus(
            query=str(args.get("topic") or ""),
            purpose="quote_discovery",
            source_ids=source_ids or [str(item) for item in args.get("source_ids") or [] if item],
            limit=int(args.get("limit") or 5),
            scope="selected_sources" if source_ids else "all_sources",
        )
        result["results"] = [item for item in result["results"] if len(str(item.get("short_snippet") or "")) >= 40]
        return result

    async def search_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 8), 8))
        evidence = await self.search_corpus(query=query, purpose="source_map", source_ids=[], limit=limit, scope="all_sources")
        sources = await _source_summaries(self.db, self.run_view, evidence["results"], limit=limit)
        return {
            "accepted": True,
            "query": query[:300],
            "returned_source_count": len(sources),
            "returned_evidence_count": len(evidence["results"]),
            "sources": sources,
            "evidence": evidence["results"][:limit],
        }

    async def compare_source_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        guard_error = await self._triage_read_guard_error()
        if guard_error:
            return {**guard_error, "comparisons": []}
        requested_refs = [str(item) for item in args.get("source_refs") or [] if item]
        scope_error = await self._triage_scope_error_for_refs(requested_refs)
        if scope_error:
            return {**scope_error, "comparisons": []}
        requested_source_ids, invalid = await self._source_ids_from_refs_or_ids(requested_refs)
        if invalid:
            return {"accepted": False, "error": "source_refs include values outside this run corpus view", "invalid_source_refs": invalid, "comparisons": []}
        if len(requested_source_ids) < 2:
            return {"accepted": False, "error": "compare_source_evidence requires at least two source_refs", "comparisons": []}
        source_ids = requested_source_ids[:5]
        question = str(args.get("question") or "")
        limit_per_source = max(1, min(int(args.get("limit_per_source") or 2), 3))
        comparisons = []
        for source_id in source_ids:
            result = await self.search_corpus(
                query=question,
                purpose="source_compare",
                source_ids=[source_id],
                limit=limit_per_source,
                scope="selected_sources",
            )
            comparisons.append({"source_ref": await self._source_ref_for_id(source_id), "evidence": result.get("results", [])})
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="compare_source_evidence",
            input_json={"source_ids": source_ids, "question": question, "limit_per_source": limit_per_source},
            output_json={
                "evidence_ids": [
                    item["evidence_id"]
                    for comparison in comparisons
                    for item in comparison["evidence"]
                    if item.get("evidence_id")
                ]
            },
        )
        return {
            "accepted": True,
            "question": question[:300],
            "comparisons": comparisons,
            "requested_source_count": len(requested_source_ids),
            "compared_source_count": len(source_ids),
            "omitted_source_count": max(0, len(requested_source_ids) - len(source_ids)),
            "expected_next_action": "record_triage_observation or write a source cluster/relationship/gap for this source set before starting another comparison.",
        }

    async def select_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
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
        item = await self._mark_evidence_status(
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
        source_ref = str(args.get("source_ref") or args.get("source_id") or "")
        scope_error = await self._triage_scope_error_for_refs([source_ref] if source_ref else [])
        if scope_error:
            return scope_error
        source_id = await self._source_id_from_ref_or_id(source_ref) or ""
        if not self._source_in_run_view(source_id):
            return {"accepted": False, "error": "source_ref is not in this run corpus view"}
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._select_evidence_ids_for_write(
            evidence_ids,
            source_id=source_id,
            purpose="source_card",
            role="triage_source_card",
            rationale="Used by upsert_source_card.",
        )
        if invalid:
            return {
                "accepted": False,
                "error": "evidence_ids must be observed evidence from source_ref in this run",
                "invalid_evidence_ids": invalid,
            }
        if not evidence_ids:
            return {"accepted": False, "error": "upsert_source_card requires at least one evidence_id from read/search tools"}
        resolved_ref = await self._source_ref_for_id(source_id)
        await mark_workspace_write_action(
            self.db,
            generation_run_id=self.run.id,
            tool_name="upsert_source_card",
            source_refs=[resolved_ref],
            evidence_ids=evidence_ids,
        )
        return {"accepted": True, "source_ref": resolved_ref, "evidence_count": len(evidence_ids)}

    async def create_quote_card_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("source_id") or "")
        source_version_id = str(args.get("source_version_id") or "")
        evidence_id = str(args.get("evidence_id") or "")
        quote = str(args.get("quote") or "").strip()
        locator = str(args.get("locator") or "").strip()
        if not quote:
            return {"accepted": False, "error": "quote is required"}
        if not locator:
            return {"accepted": False, "error": "locator is required"}
        evidence = await self._evidence_in_run(evidence_id)
        if evidence is None:
            return {"accepted": False, "error": "evidence_id must exist in this run before creating a quote card"}
        resolved_ref = await self._source_ref_for_id(evidence.source_id)
        scope_error = await self._triage_scope_error_for_refs([resolved_ref])
        if scope_error:
            return scope_error
        if source_id and str(evidence.source_id) != source_id:
            return {"accepted": False, "error": "provided source does not match selected evidence"}
        if source_version_id and str(evidence.source_version_id) != source_version_id:
            return {"accepted": False, "error": "provided source version does not match selected evidence"}
        if not _quote_text_matches_evidence(quote, evidence.snippet_text):
            return {"accepted": False, "error": "quote text is not found in selected evidence snippet/window"}
        invalid = await self._select_evidence_ids_for_write(
            [evidence_id],
            purpose="quote_card",
            role="triage_quote_seed",
            rationale="Used by create_quote_card.",
        )
        if invalid:
            return {"accepted": False, "error": "quote evidence could not be selected", "invalid_evidence_ids": invalid}
        await mark_workspace_write_action(
            self.db,
            generation_run_id=self.run.id,
            tool_name="create_quote_card",
            source_refs=[resolved_ref],
            evidence_ids=[evidence_id],
        )
        return {"accepted": True, "source_ref": resolved_ref, "evidence_id": evidence_id}

    async def record_source_relationship_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_refs = [str(args.get("source_ref") or args.get("source_id") or ""), str(args.get("related_source_ref") or args.get("related_source_id") or "")]
        scope_error = await self._triage_scope_error_for_refs([ref for ref in raw_refs if ref])
        if scope_error:
            return scope_error
        source_id = await self._source_id_from_ref_or_id(str(args.get("source_ref") or args.get("source_id") or "")) or ""
        related_source_id = await self._source_id_from_ref_or_id(str(args.get("related_source_ref") or args.get("related_source_id") or "")) or ""
        if not self._source_in_run_view(source_id) or not self._source_in_run_view(related_source_id):
            return {"accepted": False, "error": "source_ref and related_source_ref must be in this run corpus view"}
        relationship = _relationship_kind(str(args.get("relationship") or ""))
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._select_evidence_ids_for_write(
            evidence_ids,
            purpose="source_relationship",
            role=f"triage_relationship:{relationship}",
            rationale="Used by record_source_relationship.",
        )
        if invalid:
            return {
                "accepted": False,
                "error": "relationship evidence_ids must be observed evidence from this run",
                "invalid_evidence_ids": invalid,
            }
        if relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"} and not evidence_ids:
            return {"accepted": False, "error": f"{relationship} relationship requires evidence_ids from read/search tools"}
        await mark_workspace_write_action(
            self.db,
            generation_run_id=self.run.id,
            tool_name="record_source_relationship",
            source_refs=[await self._source_ref_for_id(source_id), await self._source_ref_for_id(related_source_id)],
            evidence_ids=evidence_ids,
        )
        return {"accepted": True, "relationship": relationship, "evidence_count": len(evidence_ids)}

    async def record_source_cluster_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_refs = [str(item) for item in args.get("source_refs") or args.get("source_ids") or [] if item]
        primary_refs = [str(item) for item in args.get("primary_source_refs") or [] if item]
        secondary_refs = [str(item) for item in args.get("secondary_source_refs") or [] if item]
        duplicate_refs = [str(item) for item in args.get("duplicate_source_refs") or [] if item]
        scope_error = await self._triage_scope_error_for_refs([*source_refs, *primary_refs, *secondary_refs, *duplicate_refs])
        if scope_error:
            return scope_error
        source_ids, invalid_sources = await self._source_ids_from_refs_or_ids(source_refs)
        primary_ids, invalid_primary = await self._source_ids_from_refs_or_ids(primary_refs)
        secondary_ids, invalid_secondary = await self._source_ids_from_refs_or_ids(secondary_refs)
        duplicate_ids, invalid_duplicate = await self._source_ids_from_refs_or_ids(duplicate_refs)
        invalid_refs = [*invalid_sources, *invalid_primary, *invalid_secondary, *invalid_duplicate]
        if invalid_refs:
            return {"accepted": False, "error": "cluster source_refs must be in this run corpus view", "invalid_source_refs": invalid_refs}
        label = str(args.get("label") or "").strip()
        rationale = str(args.get("rationale") or "").strip()
        if not label:
            return {"accepted": False, "error": "label is required"}
        if len(source_ids) < 2:
            return {"accepted": False, "error": "record_source_cluster requires at least two source_refs"}
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._select_evidence_ids_for_write(
            evidence_ids,
            purpose="source_cluster",
            role="triage_source_cluster",
            rationale="Used by record_source_cluster.",
        )
        if invalid:
            return {
                "accepted": False,
                "error": "cluster evidence_ids must be observed evidence from this run",
                "invalid_evidence_ids": invalid,
            }
        if (primary_ids or secondary_ids or duplicate_ids) and not evidence_ids:
            return {"accepted": False, "error": "primary/secondary/duplicate cluster claims require evidence_ids"}
        resolved_refs = [await self._source_ref_for_id(source_id) for source_id in source_ids]
        await mark_workspace_write_action(
            self.db,
            generation_run_id=self.run.id,
            tool_name="record_source_cluster",
            source_refs=resolved_refs,
            evidence_ids=evidence_ids,
        )
        return {
            "accepted": True,
            "source_count": len(source_ids),
            "primary_source_count": len(primary_ids),
            "secondary_source_count": len(secondary_ids),
            "duplicate_source_count": len(duplicate_ids),
            "evidence_count": len(evidence_ids),
            "has_rationale": bool(rationale),
        }

    async def record_gap_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_refs = [str(item) for item in args.get("source_refs") or args.get("source_ids") or [] if item]
        scope_error = await self._triage_scope_error_for_refs(source_refs)
        if scope_error:
            return scope_error
        source_ids, invalid_sources = await self._source_ids_from_refs_or_ids(source_refs)
        if invalid_sources:
            return {"accepted": False, "error": "source_refs must be in this run corpus view", "invalid_source_refs": invalid_sources}
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._select_evidence_ids_for_write(
            evidence_ids,
            purpose="gap",
            role=f"triage_gap:{str(args.get('kind') or 'gap')}",
            rationale="Used by record_gap.",
        )
        if invalid:
            return {"accepted": False, "error": "gap evidence_ids must be observed evidence from this run", "invalid_evidence_ids": invalid}
        if not str(args.get("description") or "").strip():
            return {"accepted": False, "error": "description is required"}
        resolved_refs = [await self._source_ref_for_id(source_id) for source_id in source_ids]
        await mark_workspace_write_action(
            self.db,
            generation_run_id=self.run.id,
            tool_name="record_gap",
            source_refs=resolved_refs,
            evidence_ids=evidence_ids,
        )
        return {"accepted": True, "source_count": len(source_ids), "evidence_count": len(evidence_ids)}

    async def finalize_source_map_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        if not bool(args.get("ready")):
            return {"accepted": False, "error": "finalize_source_map ready=false; record gaps first or keep triage unfinished"}
        if not str(args.get("summary") or "").strip():
            return {"accepted": False, "error": "summary is required"}
        selected = await self._selected_evidence_ids()
        if not selected:
            return {"accepted": False, "error": "cannot finalize source map without selected evidence"}
        return {"accepted": True, "selected_evidence_count": len(selected)}

    async def persist_hosted_file_search_calls(
        self,
        calls: list[dict[str, Any]],
        response_id: str,
        phase: str,
        agent_name: str,
    ) -> dict[str, Any]:
        observed: list[dict[str, Any]] = []
        for call in calls:
            for result in call.get("results") or []:
                evidence = await self._persist_hosted_file_search_result(
                    result,
                    response_id=response_id,
                    tool_call_id=str(call.get("call_id") or ""),
                    query_text=", ".join(str(item) for item in call.get("queries") or []),
                    result_index=int(result.get("result_index") or 0),
                )
                if evidence:
                    observed.append(await self._evidence_preview_payload(evidence, max_short_snippet_chars=420))
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=phase,
            agent_name=agent_name,
            tool_name="openai.file_search.observed",
            input_json={"response_id": response_id, "call_count": len(calls)},
            output_json={
                "observed_evidence_ids": [item["evidence_id"] for item in observed],
                "observed_count": len(observed),
            },
            provider_call_id=response_id,
        )
        return {
            "observed_evidence_ids": [item["evidence_id"] for item in observed],
            "observed_evidence": observed,
            "observed_count": len(observed),
        }

    async def _canonical_kinds_by_source(self) -> dict[str, str]:
        canonical_ids = [uuid.UUID(item) for item in _active_filter_list(self.run_view, "canonical_source_version_ids") if _uuid_or_none(item)]
        if not canonical_ids:
            return {}
        result = await self.db.execute(select(SourceVersion).where(SourceVersion.id.in_(canonical_ids)))
        return {str(version.source_id): str(version.kind) for version in result.scalars()}

    async def _persist_hosted_file_search_result(
        self,
        result: dict[str, Any],
        *,
        response_id: str,
        tool_call_id: str,
        query_text: str,
        result_index: int,
    ) -> EvidenceItem | None:
        if self.provider_index is None:
            return None
        provider_file_id = _hosted_result_file_id(result)
        if not provider_file_id:
            return None
        file_result = await self.db.execute(
            select(ProviderCorpusFile).where(
                ProviderCorpusFile.provider_index_id == self.provider_index.id,
                ProviderCorpusFile.provider_file_id == provider_file_id,
            )
        )
        provider_file = file_result.scalar_one_or_none()
        if provider_file is None:
            return None
        if not self._in_run_corpus_view(provider_file.source_id, provider_file.source_version_id):
            return None
        eligible_provider_file_ids = {
            str(item) for item in getattr(self.run_view, "eligible_provider_file_ids", []) if item
        }
        if provider_file.provider_file_id not in eligible_provider_file_ids:
            return None
        snippet = _hosted_result_text(result)
        if not snippet.strip():
            return None
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=provider_file.source_id,
            source_version_id=provider_file.source_version_id,
            provider="openai",
            provider_vector_store_id=self.provider_index.provider_vector_store_id if self.provider_index else None,
            provider_file_id=provider_file.provider_file_id,
            provider_vector_store_file_id=provider_file.provider_vector_store_file_id,
            response_id=response_id,
            tool_call_id=tool_call_id,
            retrieval_channel=EvidenceRetrievalChannel.PROVIDER_FILE_SEARCH,
            query_text=query_text,
            purpose="hosted_file_search",
            search_result_index=result_index,
            score=_confidence(result.get("score")),
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet[:500]),
            locator_text=str(result.get("filename") or "file_search snippet"),
            locator_type=EvidenceLocatorType.PROVIDER_SNIPPET,
            evidence_strength=EvidenceStrength.SNIPPET,
            metadata={
                "usage_status": EVIDENCE_STATUS_OBSERVED,
                "hosted_file_search": True,
                "result": _safe_metadata(result),
            },
        )

    async def _mark_evidence_status(
        self,
        evidence_id: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceItem | None:
        if not _uuid_or_none(evidence_id):
            return None
        item = await self.db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != self.run.id:
            return None
        item.metadata_ = {
            **(item.metadata_ or {}),
            "usage_status": status,
            **(metadata or {}),
        }
        await self.db.flush()
        return item

    async def _evidence_in_run(self, evidence_id: str) -> EvidenceItem | None:
        if not _uuid_or_none(evidence_id):
            return None
        item = await self.db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != self.run.id:
            return None
        if not self._in_run_corpus_view(item.source_id, item.source_version_id):
            return None
        return item

    def _source_in_run_view(self, source_id: str) -> bool:
        return bool(_uuid_or_none(source_id) and source_id in {str(item) for item in self.run_view.selected_source_ids})

    def _source_version_in_run_view(self, source_version_id: str) -> bool:
        return bool(
            _uuid_or_none(source_version_id)
            and source_version_id in {str(item) for item in self.run_view.selected_source_version_ids}
        )

    async def _selected_evidence(self, evidence_id: str) -> EvidenceItem | None:
        if not _uuid_or_none(evidence_id):
            return None
        item = await self.db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != self.run.id:
            return None
        if (item.metadata_ or {}).get("usage_status") not in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}:
            return None
        if not self._in_run_corpus_view(item.source_id, item.source_version_id):
            return None
        return item

    async def _selected_evidence_ids(self) -> set[str]:
        result = await self.db.execute(
            select(EvidenceItem).where(
                EvidenceItem.generation_run_id == self.run.id,
            )
        )
        return {
            str(item.id)
            for item in result.scalars()
            if (item.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
            and self._in_run_corpus_view(item.source_id, item.source_version_id)
        }

    async def _invalid_selected_evidence_ids(self, evidence_ids: list[str], *, source_id: str | None = None) -> list[str]:
        invalid: list[str] = []
        for evidence_id in _dedupe(evidence_ids):
            item = await self._selected_evidence(evidence_id)
            if item is None:
                invalid.append(evidence_id)
                continue
            if source_id and str(item.source_id) != source_id:
                invalid.append(evidence_id)
        return invalid

    async def _select_evidence_ids_for_write(
        self,
        evidence_ids: list[str],
        *,
        source_id: str | None = None,
        purpose: str,
        role: str,
        rationale: str,
    ) -> list[str]:
        invalid: list[str] = []
        for evidence_id in _dedupe(evidence_ids):
            item = await self._evidence_in_run(evidence_id)
            if item is None:
                invalid.append(evidence_id)
                continue
            if source_id and str(item.source_id) != str(source_id):
                invalid.append(evidence_id)
                continue
            if _unsupported_model_read_selected_as_support(item, purpose=purpose, role=role):
                invalid.append(evidence_id)
                continue
            await self._mark_evidence_status(
                evidence_id,
                EVIDENCE_STATUS_SELECTED,
                {
                    "selected_purpose": purpose,
                    "selected_role": role,
                    "selected_rationale": rationale,
                },
            )
        return invalid


def _compact_source_card(card: Any, *, canonical_kind: str | None = None, source_ref: str) -> dict[str, Any]:
    canonical_kind = canonical_kind or _first_representation_kind(card)
    return {
        "source_ref": source_ref,
        "title": str(card.title or "")[:160].rstrip(),
        "source_type": card.source_type,
        "domain": card.domain,
        "published_at": card.published_at,
        "coverage_status": card.coverage_status,
        "canonical_kind": canonical_kind,
        "version_count": len(card.source_version_ids or []),
        "brief_hint": _brief_hint(card),
    }


def _active_filter_list(run_view: Any, key: str) -> list[str]:
    active_filter = getattr(run_view, "active_source_filter", None) or {}
    values = active_filter.get(key) or []
    return [str(item) for item in values if item]


def _first_representation_kind(card: Any) -> str:
    summary = str(getattr(card, "representation_summary", "") or "")
    first = summary.split(",", 1)[0].strip()
    if ":" in first:
        return first.split(":", 1)[0].strip()
    return first or "unknown"


def _brief_hint(card: Any) -> str:
    text = " ".join(str(getattr(card, "summary", "") or "").split())
    return text[:120].rstrip()


def _cached_model_read_payload(evidence: EvidenceItem) -> dict[str, Any]:
    metadata = evidence.metadata_ or {}
    payload = _model_read_agent_payload(
        {
            "supports_query": metadata.get("supports_query"),
            "support_level": metadata.get("support_level"),
            "locator_text": evidence.locator_text,
            "locator_type": evidence.locator_type,
            "confidence": evidence.score,
        }
    )
    payload["cache_hit"] = True
    return payload


def _model_read_agent_payload(read_result: dict[str, Any]) -> dict[str, Any]:
    compact = compact_model_read_result(read_result)
    if "support_level" in compact:
        compact["support_assessment"] = compact.pop("support_level")
    if "supports_query" in compact:
        compact["supports_query"] = compact["supports_query"]
    return compact


def _coverage_warning(item: EvidenceItem) -> str:
    metadata = item.metadata_ or {}
    if metadata.get("model_file_read_local_fallback"):
        return "Direct uploaded-file read was unavailable; this is a local fallback window and should be treated cautiously unless confirmed elsewhere."
    if metadata.get("source_version_fallback") or item.locator_type == EvidenceLocatorType.SOURCE_LEVEL:
        return "This is a broad source-level snapshot, not a precise cited passage; read a concrete source window before using it for an important claim."
    if metadata.get("fallback"):
        return "This came from local text/chunk fallback; use it as a lead and confirm important claims with a focused read."
    return ""


__all__ = ["EvidenceToolHandlersMixin"]
