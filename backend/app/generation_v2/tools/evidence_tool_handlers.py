from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
    SourceVersion,
)
from app.generation_v2.tools.evidence_source_map import _relationship_kind, _source_summaries, build_source_inventory
from app.generation_v2.tools.evidence_store import evidence_payload, persist_evidence_item
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
    _version_readable,
)


class EvidenceToolHandlersMixin:
    async def list_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit") or 50), 100))
        cursor = str(args.get("cursor") or "")
        offset = int(cursor) if cursor.isdigit() else 0
        inventory = await build_source_inventory(self.db, self.run_view)
        selected = inventory.source_cards[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(inventory.source_cards) else ""
        return {
            "sources": [card.model_dump(mode="json") for card in selected],
            "next_cursor": next_cursor,
            "total": len(inventory.source_cards),
        }

    async def list_source_versions_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("source_id") or "")
        if not _uuid_or_none(source_id) or source_id not in {str(item) for item in self.run_view.selected_source_ids}:
            return {"found": False, "error": "source_id is not in this run corpus view"}
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.source_id == uuid.UUID(source_id),
                SourceVersion.id.in_(_uuid_list([str(item) for item in self.run_view.selected_source_version_ids])),
            )
        )
        versions = sorted(list(result.scalars()), key=lambda version: _readable_version_priority(version.kind))
        return {
            "found": bool(versions),
            "source_id": source_id,
            "versions": [
                {
                    "source_version_id": str(version.id),
                    "kind": version.kind,
                    "status": version.status,
                    "upload_status": version.upload_status,
                    "provider_file_id": version.provider_file_id,
                    "provider_vector_store_file_id": version.provider_vector_store_file_id,
                    "content_hash": version.content_hash,
                    "readable_locally": _version_readable(version),
                    "storage_path": version.storage_path,
                    "metadata": version.metadata_,
                }
                for version in versions
            ],
        }

    async def search_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self.search_evidence(
            query=str(args.get("query") or ""),
            purpose=str(args.get("purpose") or "general"),
            source_ids=[str(item) for item in args.get("source_ids") or [] if item],
            limit=int(args.get("limit") or 8),
        )

    async def read_evidence_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            evidence_id = uuid.UUID(str(args.get("evidence_id") or ""))
        except ValueError:
            return {"found": False, "error": "Invalid evidence_id"}
        item = await self.db.get(EvidenceItem, evidence_id)
        if item is None or item.generation_run_id != self.run.id:
            return {"found": False}
        return {"found": True, "evidence": evidence_payload(item)}

    async def inspect_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        max_items = max(1, min(int(args.get("max_items") or 40), 80))
        text = _source_version_text(version)
        items = _source_outline(text, max_items=max_items) if text.strip() else []
        if not items:
            chunks = await self._chunks_for_source(str(version.source_id), limit=max_items)
            items = [
                {
                    "locator": " > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}",
                    "locator_type": "section" if chunk.heading_path else "chunk",
                    "preview": chunk.content[:240],
                }
                for chunk in chunks
            ]
        if not items:
            windows = await self._source_windows(version, max_windows=min(max_items, 12))
            items = [
                {
                    "locator": item["locator_text"],
                    "locator_type": item["locator_type"],
                    "preview": item["snippet_text"][:240],
                }
                for item in windows
            ]
        return {
            "found": bool(items),
            "source_id": str(version.source_id),
            "source_version_id": str(version.id),
            "kind": version.kind,
            "items": items,
        }

    async def read_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        locator = str(args.get("locator") or "")
        max_chars = max(200, min(int(args.get("max_chars") or 2400), 6000))
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
        payload = evidence_payload(evidence)
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="read_source",
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "locator": locator,
                "max_chars": max_chars,
            },
            output_json={"evidence_id": payload["id"], "locator_text": payload["locator_text"]},
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
        provider_failures: list[dict[str, Any]] = []
        candidates: list[tuple[ProviderCorpusFile, SourceVersion]] = []
        if settings.openai_api_key:
            candidates = await self._resolve_provider_file_candidates_for_model_read(
                source_id=source_id,
                source_version_id=source_version_id,
            )

        for provider_file, version in candidates:
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
                    "provider_file_id": provider_file.provider_file_id,
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
                        "provider_file_id": provider_file.provider_file_id,
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
            )
            payload = evidence_payload(evidence)
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
                },
                output_json={"evidence_id": payload["id"], "support_level": read_result.get("support_level")},
                provider_call_id=response_id,
            )
            return {"found": True, "evidence": payload, "model_read": read_result}

        return await self._model_read_local_fallback(
            source_id=source_id,
            source_version_id=source_version_id,
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
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
        provider_failures: list[dict[str, Any]],
        provider_candidate_count: int,
    ) -> dict[str, Any]:
        version = await self._resolve_local_fallback_source_version(source_id=source_id, source_version_id=source_version_id)
        if version is None:
            return {
                "found": False,
                "error": "No provider-backed or locally readable source version is available in this run corpus view.",
                "provider_failures": provider_failures,
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
                "provider_failures": provider_failures,
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
            },
        )
        payload = evidence_payload(evidence)
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
            },
            output_json={
                "evidence_id": payload["id"],
                "local_fallback": True,
                "local_read_channel": channel,
                "provider_failures": provider_failures,
            },
        )
        return {"found": True, "evidence": payload, "local_fallback": True, "provider_failures": provider_failures}

    async def search_within_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
        )
        if version is None:
            return {"results": [], "error": "No readable source version in this run corpus view."}
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 6), 12))
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
                results.append(evidence_payload(evidence))
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="search_within_source",
                input_json={"source_id": str(version.source_id), "source_version_id": str(version.id), "query": query, "limit": limit},
                output_json={"evidence_ids": [item["id"] for item in results], "result_count": len(results)},
            )
            return {"results": results}
        return await self.search_evidence(query=query, purpose="source_internal_search", source_ids=[str(version.source_id)], limit=limit)

    async def skim_source_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        version = await self._resolve_source_version(
            source_id=str(args.get("source_id") or ""),
            source_version_id=str(args.get("source_version_id") or ""),
        )
        if version is None:
            return {"found": False, "error": "No readable source version in this run corpus view."}
        max_windows = max(1, min(int(args.get("max_windows") or 6), 12))
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
            results.append({**window, "evidence": evidence_payload(evidence)})
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="skim_source",
            input_json={"source_id": str(version.source_id), "source_version_id": str(version.id), "max_windows": max_windows},
            output_json={"evidence_ids": [item["evidence"]["id"] for item in results], "window_count": len(results)},
        )
        return {
            "found": bool(results),
            "source_id": str(version.source_id),
            "source_version_id": str(version.id),
            "outline": [item["locator_text"] for item in results],
            "windows": results,
        }

    async def find_quotes_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        result = await self.search_evidence(
            query=str(args.get("topic") or ""),
            purpose="quote_discovery",
            source_ids=[str(item) for item in args.get("source_ids") or [] if item],
            limit=int(args.get("limit") or 8),
        )
        result["results"] = [item for item in result["results"] if len(str(item.get("snippet_text") or "")) >= 40]
        return result

    async def search_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "")
        limit = int(args.get("limit") or 20)
        evidence = await self.search_evidence(query=query, purpose="source_map", source_ids=[], limit=limit)
        sources = await _source_summaries(self.db, self.run_view, evidence["results"])
        return {"sources": sources, "evidence": evidence["results"]}

    async def compare_sources_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_ids = [str(item) for item in args.get("source_ids") or [] if item]
        question = str(args.get("question") or "")
        limit_per_source = max(1, min(int(args.get("limit_per_source") or 3), 6))
        comparisons = []
        for source_id in source_ids:
            result = await self.search_evidence(
                query=question,
                purpose="source_compare",
                source_ids=[source_id],
                limit=limit_per_source,
            )
            comparisons.append({"source_id": source_id, "evidence": result.get("results", [])})
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="compare_sources",
            input_json={"source_ids": source_ids, "question": question, "limit_per_source": limit_per_source},
            output_json={
                "evidence_ids": [
                    item["id"]
                    for comparison in comparisons
                    for item in comparison["evidence"]
                    if item.get("id")
                ]
            },
        )
        return {"question": question, "comparisons": comparisons}

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
        return {
            "accepted": True,
            "evidence_id": evidence_id,
            "usage_status": EVIDENCE_STATUS_SELECTED,
            "evidence": evidence_payload(item),
        }

    async def write_action_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"accepted": True, "action": args}

    async def upsert_source_card_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("source_id") or "")
        if not self._source_in_run_view(source_id):
            return {"accepted": False, "error": "source_id is not in this run corpus view"}
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._invalid_selected_evidence_ids(evidence_ids, source_id=source_id)
        if invalid:
            return {"accepted": False, "error": "evidence_ids must be selected and belong to source_id", "invalid_evidence_ids": invalid}
        if not evidence_ids:
            return {"accepted": False, "error": "upsert_source_card requires at least one selected evidence_id"}
        return {"accepted": True, "action": args}

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
        evidence = await self._selected_evidence(evidence_id)
        if evidence is None:
            return {"accepted": False, "error": "evidence_id must exist and be selected before creating a quote card"}
        if source_id and str(evidence.source_id) != source_id:
            return {"accepted": False, "error": "source_id does not match selected evidence", "evidence_source_id": str(evidence.source_id)}
        if source_version_id and str(evidence.source_version_id) != source_version_id:
            return {
                "accepted": False,
                "error": "source_version_id does not match selected evidence",
                "evidence_source_version_id": str(evidence.source_version_id),
            }
        if not _quote_text_matches_evidence(quote, evidence.snippet_text):
            return {"accepted": False, "error": "quote text is not found in selected evidence snippet/window"}
        return {"accepted": True, "action": args}

    async def record_source_relationship_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_id = str(args.get("source_id") or "")
        related_source_id = str(args.get("related_source_id") or "")
        if not self._source_in_run_view(source_id) or not self._source_in_run_view(related_source_id):
            return {"accepted": False, "error": "source_id and related_source_id must be in this run corpus view"}
        relationship = _relationship_kind(str(args.get("relationship") or ""))
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._invalid_selected_evidence_ids(evidence_ids)
        if invalid:
            return {"accepted": False, "error": "relationship evidence_ids must be selected", "invalid_evidence_ids": invalid}
        if relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"} and not evidence_ids:
            return {"accepted": False, "error": f"{relationship} relationship requires selected evidence_ids"}
        return {"accepted": True, "action": args}

    async def record_gap_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        source_ids = [str(item) for item in args.get("source_ids") or [] if item]
        invalid_sources = [source_id for source_id in source_ids if not self._source_in_run_view(source_id)]
        if invalid_sources:
            return {"accepted": False, "error": "source_ids must be in this run corpus view", "invalid_source_ids": invalid_sources}
        evidence_ids = [str(item) for item in args.get("evidence_ids") or [] if item]
        invalid = await self._invalid_selected_evidence_ids(evidence_ids)
        if invalid:
            return {"accepted": False, "error": "gap evidence_ids must be selected", "invalid_evidence_ids": invalid}
        if not str(args.get("description") or "").strip():
            return {"accepted": False, "error": "description is required"}
        return {"accepted": True, "action": args}

    async def finalize_source_map_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        if not bool(args.get("ready")):
            return {"accepted": False, "error": "finalize_source_map ready=false; record gaps first or keep triage unfinished"}
        if not str(args.get("summary") or "").strip():
            return {"accepted": False, "error": "summary is required"}
        selected = await self._selected_evidence_ids()
        if not selected:
            return {"accepted": False, "error": "cannot finalize source map without selected evidence"}
        return {"accepted": True, "action": args, "selected_evidence_count": len(selected)}

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
                    observed.append(evidence_payload(evidence))
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=phase,
            agent_name=agent_name,
            tool_name="openai.file_search.observed",
            input_json={"response_id": response_id, "call_count": len(calls)},
            output_json={
                "observed_evidence_ids": [item["id"] for item in observed],
                "observed_count": len(observed),
            },
            provider_call_id=response_id,
        )
        return {
            "observed_evidence_ids": [item["id"] for item in observed],
            "observed_evidence": observed,
            "observed_count": len(observed),
        }

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

__all__ = ["EvidenceToolHandlersMixin"]
