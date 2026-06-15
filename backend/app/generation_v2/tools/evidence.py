from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import QuoteCard, SourceCard, SourceMap, SourceTriageGate, SourceTriageItem, SourceTriageReport
from app.models.chunk import Chunk
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
    ProviderCorpusIndex,
    RunCorpusView,
    SourceRepresentationKind,
    SourceVersion,
    V2ToolTrace,
    V2RecordStatus,
)
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import download_text, get_minio_client


_CHUNK_BACKED_VERSION_KINDS = (
    SourceRepresentationKind.EXTRACTED_TEXT,
    SourceRepresentationKind.TRANSCRIPT,
)
_TRIAGE_READING_TOOL_NAMES = {
    "list_sources",
    "search_sources",
    "search_evidence",
    "find_quotes",
    "read_evidence",
    "read_source",
    "skim_source",
    "compare_sources",
}


@dataclass
class EvidenceToolset:
    db: AsyncSession
    run: GenerationRun
    run_view: RunCorpusView
    provider_index: ProviderCorpusIndex | None

    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                name="list_sources",
                description=(
                    "List neutral metadata for sources selected in this run: source_id, title/url/domain, type, date, "
                    "active source_version_ids, provider status, authority/reliability hints, and a short preview. "
                    "Use this at the start of source triage to plan which sources to skim/read. It does not create evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "filters": {"type": "object", "additionalProperties": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "cursor": {"type": "string"},
                    },
                    "required": ["filters", "limit", "cursor"],
                },
                handler=self.list_sources_tool,
            ),
            AgentTool(
                name="search_evidence",
                description=(
                    "Search the selected run corpus for source-backed evidence and persist every returned result as an EvidenceItem. "
                    "Use this when you need support for a specific editorial atom, act gap, quote, or citation-sensitive claim. "
                    "Pass source_ids=[] to search all sources selected for this run; pass source_ids to target known sources. "
                    "Returned evidence_id values are authoritative and must be copied into atoms, quote refs, citations, and repairs. "
                    "For final required factual claims, use evidence_strength exact or snippet; source_level or weak evidence is not sufficient. "
                    "Provider retrieval channels generally mean OpenAI vector/file search found a saved source version; local fallback channels are usable but should be treated with more caution and checked for locator strength."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "purpose": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query", "purpose", "source_ids", "limit"],
                },
                handler=self.search_evidence_tool,
            ),
            AgentTool(
                name="read_evidence",
                description=(
                    "Read one already-saved EvidenceItem by evidence_id. Use this to inspect selected evidence before writing, rewriting, or linking a claim. "
                    "Copy source_id, source_version_id, locator_text, snippet_text, retrieval_channel, evidence_strength, and evidence_id exactly from this result when citing it. "
                    "Do not use read_evidence to discover new material; call search_evidence first if no evidence_id is known."
                ),
                parameters={
                    "type": "object",
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
                handler=self.read_evidence_tool,
            ),
            AgentTool(
                name="read_source",
                description=(
                    "Read a concrete window from a selected source/source version and persist that window as an EvidenceItem. "
                    "Use source_id when you want the best readable version, or source_version_id when you need a specific version. "
                    "locator may be a heading/page/timestamp/search phrase; leave empty to read the beginning. "
                    "Returned evidence_id, source_id, source_version_id, locator_text, snippet_text, retrieval_channel, and evidence_strength are the values to copy into SourceMap, QuoteCard, atoms, or citations."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "locator": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 6000},
                    },
                    "required": ["source_id", "source_version_id", "locator", "max_chars"],
                },
                handler=self.read_source_tool,
            ),
            AgentTool(
                name="skim_source",
                description=(
                    "Skim one selected source/source version and persist representative windows as EvidenceItems. "
                    "Use this before judging a source's role, structure, likely authority, duplicate status, or editorial usefulness. "
                    "The result includes outline/section labels and evidence_id-backed windows; copy those evidence ids into the SourceMap when used."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "max_windows": {"type": "integer", "minimum": 1, "maximum": 12},
                    },
                    "required": ["source_id", "source_version_id", "max_windows"],
                },
                handler=self.skim_source_tool,
            ),
            AgentTool(
                name="find_quotes",
                description=(
                    "Find quote-like snippets about a specific topic and persist them as EvidenceItems. "
                    "Use during source triage, research, and citation linking when a direct quote or quote-like support is needed. "
                    "This is not a generic source search tool; for general evidence use search_evidence. "
                    "Quote refs should be evidence_id values or source_id:locator refs from returned evidence, never raw quote text."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["topic", "source_ids", "limit"],
                },
                handler=self.find_quotes_tool,
            ),
            AgentTool(
                name="search_sources",
                description=(
                    "Search for source-level candidates and evidence-backed snippets for corpus/source triage only. "
                    "Use this to rank sources, detect source clusters, identify primary/secondary or duplicate relationships, and find gaps. "
                    "Do not use search_sources in treatment, writing, rewriting, critique, or final citation linking."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                    },
                    "required": ["query", "limit"],
                },
                handler=self.search_sources_tool,
            ),
            AgentTool(
                name="compare_sources",
                description=(
                    "Compare selected sources for a specific question during triage: duplicate/near-duplicate, primary vs secondary, updates, contradictions, or coverage gaps. "
                    "Internally retrieves and persists evidence per source, then returns evidence_id-backed comparison inputs. "
                    "Use it only in source triage/research, not drafting or final citation linking."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "question": {"type": "string"},
                        "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 6},
                    },
                    "required": ["source_ids", "question", "limit_per_source"],
                },
                handler=self.compare_sources_tool,
            ),
        ]

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

    async def search_evidence(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: list[str],
        limit: int,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 20))
        provider_items = await self._provider_search(query=query, purpose=purpose, source_ids=source_ids, limit=limit)
        items = provider_items
        if len(items) < limit:
            items.extend(await self._local_fallback_search(query=query, purpose=purpose, source_ids=source_ids, limit=limit - len(items)))
        payloads = [evidence_payload(item) for item in items[:limit]]
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="search_evidence",
            input_json={"query": query, "purpose": purpose, "source_ids": source_ids, "limit": limit},
            output_json={"evidence_ids": [item["id"] for item in payloads], "result_count": len(payloads)},
        )
        return {"results": payloads}

    async def _provider_search(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: list[str],
        limit: int,
    ) -> list[EvidenceItem]:
        if not (
            settings.openai_api_key
            and self.provider_index
            and self.provider_index.provider_vector_store_id
            and self.provider_index.status == V2RecordStatus.READY
        ):
            return []
        client = get_openai_client()
        filters = _provider_filters(source_ids or [str(item) for item in self.run_view.selected_source_ids])
        results: list[EvidenceItem] = []
        try:
            idx = 0
            async for item in client.vector_stores.search(
                self.provider_index.provider_vector_store_id,
                query=query,
                filters=filters,
                max_num_results=limit,
            ):
                evidence = await self._persist_provider_result(item, query=query, purpose=purpose, result_index=idx)
                if evidence:
                    results.append(evidence)
                idx += 1
        except Exception as exc:
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="openai.vector_stores.search",
                input_json={"query": query, "source_ids": source_ids, "limit": limit},
                output_json={"error": str(exc)},
            )
        return results

    async def _persist_provider_result(self, item: Any, *, query: str, purpose: str, result_index: int) -> EvidenceItem | None:
        attrs = dict(getattr(item, "attributes", None) or {})
        source_id = _uuid_or_none(attrs.get("source_id"))
        source_version_id = _uuid_or_none(attrs.get("source_version_id"))
        if not source_id or not source_version_id:
            return None
        if not self._in_run_corpus_view(source_id, source_version_id):
            await save_tool_trace(
                self.db,
                generation_run_id=self.run.id,
                phase="v2_corpus_evidence",
                agent_name="corpus_evidence_api",
                tool_name="openai.vector_stores.search.filtered",
                input_json={
                    "source_id": str(source_id),
                    "source_version_id": str(source_version_id),
                    "reason": "outside_run_corpus_view",
                },
                output_json={"accepted": False},
            )
            return None
        snippet = _provider_snippet(item)
        if not snippet.strip():
            return None
        file_result = await self.db.execute(
            select(ProviderCorpusFile).where(
                ProviderCorpusFile.provider_index_id == self.provider_index.id,
                ProviderCorpusFile.source_version_id == source_version_id,
            )
        )
        provider_file = file_result.scalar_one_or_none()
        return await persist_evidence_item(
            self.db,
            generation_run_id=self.run.id,
            project_id=self.run.project_id,
            source_id=source_id,
            source_version_id=source_version_id,
            provider="openai",
            provider_vector_store_id=self.provider_index.provider_vector_store_id,
            provider_file_id=getattr(item, "file_id", None),
            provider_vector_store_file_id=provider_file.provider_vector_store_file_id if provider_file else None,
            retrieval_channel=EvidenceRetrievalChannel.PROVIDER_VECTOR_SEARCH,
            query_text=query,
            purpose=purpose,
            search_result_index=result_index,
            score=float(getattr(item, "score", 0.0) or 0.0),
            snippet_text=snippet,
            normalized_quote_text=_normalize(snippet[:500]),
            locator_text=str(getattr(item, "filename", "") or "provider snippet"),
            locator_type=EvidenceLocatorType.PROVIDER_SNIPPET,
            evidence_strength=EvidenceStrength.SNIPPET,
            metadata={"attributes": attrs, "filename": getattr(item, "filename", None)},
        )

    async def _local_fallback_search(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: list[str],
        limit: int,
    ) -> list[EvidenceItem]:
        if limit <= 0:
            return []
        tokens = _query_tokens(query)[:8]
        allowed_sources = set(source_ids or [str(item) for item in self.run_view.selected_source_ids])
        if not tokens:
            return await self._source_version_fallback(query=query, purpose=purpose, source_ids=allowed_sources, limit=limit)
        clauses = [Chunk.content.ilike(f"%{token}%") for token in tokens]
        result = await self.db.execute(
            select(Chunk)
            .where(
                Chunk.source_id.in_([uuid.UUID(source_id) for source_id in allowed_sources if _uuid_or_none(source_id)]),
                or_(*clauses),
            )
            .limit(max(20, limit * 4))
        )
        chunk_versions = await self._chunk_backed_versions_by_source(allowed_sources)
        candidates = []
        for chunk in result.scalars():
            version = chunk_versions.get(str(chunk.source_id))
            if version is None:
                continue
            score = sum(chunk.content.lower().count(token) for token in tokens)
            candidates.append((score, chunk, version))
        candidates.sort(key=lambda item: item[0], reverse=True)
        evidence: list[EvidenceItem] = []
        for idx, (score, chunk, version) in enumerate(candidates[:limit]):
            locator = " > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}"
            channel = (
                EvidenceRetrievalChannel.LOCAL_TRANSCRIPT_SEARCH
                if version.kind == "transcript"
                else EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH
            )
            evidence.append(
                await persist_evidence_item(
                    self.db,
                    generation_run_id=self.run.id,
                    project_id=self.run.project_id,
                    source_id=version.source_id,
                    source_version_id=version.id,
                    provider=None,
                    retrieval_channel=channel,
                    query_text=query,
                    purpose=purpose,
                    search_result_index=idx,
                    score=float(score),
                    snippet_text=chunk.content[:1600],
                    normalized_quote_text=_normalize(chunk.content[:500]),
                    locator_text=locator,
                    locator_type=EvidenceLocatorType.SECTION if chunk.heading_path else EvidenceLocatorType.UNKNOWN,
                    evidence_strength=EvidenceStrength.EXACT if chunk.heading_path else EvidenceStrength.SNIPPET,
                    metadata={"chunk_id": str(chunk.id), "fallback": True},
                )
            )
        if len(evidence) < limit:
            evidence.extend(await self._source_version_fallback(query=query, purpose=purpose, source_ids=allowed_sources, limit=limit - len(evidence)))
        return evidence

    async def _source_version_fallback(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: set[str],
        limit: int,
    ) -> list[EvidenceItem]:
        selected_version_ids = _uuid_list(self.run_view.selected_source_version_ids)
        result = await self.db.execute(
            select(SourceVersion)
            .where(
                SourceVersion.is_active.is_(True),
                SourceVersion.status == V2RecordStatus.READY,
                SourceVersion.source_id.in_([uuid.UUID(source_id) for source_id in source_ids if _uuid_or_none(source_id)]),
                SourceVersion.id.in_(selected_version_ids),
            )
            .limit(limit * 3)
        )
        evidence: list[EvidenceItem] = []
        for idx, version in enumerate(result.scalars()):
            if len(evidence) >= limit:
                break
            text = _source_version_text(version)
            if not text.strip():
                continue
            evidence.append(
                await persist_evidence_item(
                    self.db,
                    generation_run_id=self.run.id,
                    project_id=self.run.project_id,
                    source_id=version.source_id,
                    source_version_id=version.id,
                    provider=None,
                    retrieval_channel=EvidenceRetrievalChannel.WEB_SNAPSHOT if version.kind in {"fetched_html", "rendered_snapshot"} else EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH,
                    query_text=query,
                    purpose=purpose,
                    search_result_index=idx,
                    score=0.0,
                    snippet_text=text[:1600],
                    normalized_quote_text=_normalize(text[:500]),
                    locator_text="source snapshot",
                    locator_type=EvidenceLocatorType.SOURCE_LEVEL,
                    evidence_strength=EvidenceStrength.SOURCE_LEVEL,
                    metadata={"source_version_fallback": True},
                )
            )
        return evidence

    def _in_run_corpus_view(self, source_id: uuid.UUID, source_version_id: uuid.UUID) -> bool:
        return (
            str(source_id) in {str(item) for item in self.run_view.selected_source_ids}
            and str(source_version_id) in {str(item) for item in self.run_view.selected_source_version_ids}
        )

    async def _chunk_backed_versions_by_source(self, source_ids: set[str]) -> dict[str, SourceVersion]:
        selected_version_ids = _uuid_list(self.run_view.selected_source_version_ids)
        if not selected_version_ids:
            return {}
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.is_active.is_(True),
                SourceVersion.status == V2RecordStatus.READY,
                SourceVersion.source_id.in_([uuid.UUID(source_id) for source_id in source_ids if _uuid_or_none(source_id)]),
                SourceVersion.id.in_(selected_version_ids),
                SourceVersion.kind.in_(_CHUNK_BACKED_VERSION_KINDS),
            )
        )
        selected: dict[str, SourceVersion] = {}
        for version in result.scalars():
            source_id = str(version.source_id)
            current = selected.get(source_id)
            if current is None or _chunk_version_priority(version.kind) < _chunk_version_priority(current.kind):
                selected[source_id] = version
        return selected

    async def _resolve_source_version(self, *, source_id: str, source_version_id: str) -> SourceVersion | None:
        selected_version_ids = set(str(item) for item in self.run_view.selected_source_version_ids)
        selected_source_ids = set(str(item) for item in self.run_view.selected_source_ids)
        if source_version_id and _uuid_or_none(source_version_id):
            version = await self.db.get(SourceVersion, uuid.UUID(source_version_id))
            if version and str(version.id) in selected_version_ids and str(version.source_id) in selected_source_ids:
                return version
            return None
        if not source_id or not _uuid_or_none(source_id) or source_id not in selected_source_ids:
            return None
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.source_id == uuid.UUID(source_id),
                SourceVersion.id.in_(_uuid_list(list(selected_version_ids))),
                SourceVersion.status == V2RecordStatus.READY,
                SourceVersion.is_active.is_(True),
            )
        )
        versions = list(result.scalars())
        versions.sort(key=lambda version: _readable_version_priority(version.kind))
        return versions[0] if versions else None

    async def _read_source_window(
        self,
        version: SourceVersion,
        *,
        locator: str,
        max_chars: int,
    ) -> tuple[str, str, str, str]:
        text = _source_version_text(version)
        if text.strip():
            snippet, locator_text = _window_for_locator(text, locator=locator, max_chars=max_chars)
            return (
                snippet,
                locator_text,
                EvidenceLocatorType.SECTION if locator else EvidenceLocatorType.SOURCE_LEVEL,
                _retrieval_channel_for_version(version),
            )
        chunks = await self._chunks_for_source(str(version.source_id), limit=12)
        if not chunks:
            return "", "", EvidenceLocatorType.UNKNOWN, _retrieval_channel_for_version(version)
        selected = _select_chunk_window(chunks, locator=locator)
        locator_text = " > ".join(selected.heading_path or []) or f"chunk {selected.chunk_index}"
        return selected.content[:max_chars], locator_text, EvidenceLocatorType.SECTION, _retrieval_channel_for_version(version)

    async def _source_windows(self, version: SourceVersion, *, max_windows: int) -> list[dict[str, Any]]:
        chunks = await self._chunks_for_source(str(version.source_id), limit=max_windows)
        if chunks:
            return [
                {
                    "locator_text": " > ".join(chunk.heading_path or []) or f"chunk {chunk.chunk_index}",
                    "locator_type": EvidenceLocatorType.SECTION if chunk.heading_path else EvidenceLocatorType.UNKNOWN,
                    "snippet_text": chunk.content[:1600],
                    "channel": _retrieval_channel_for_version(version),
                }
                for chunk in chunks[:max_windows]
                if chunk.content.strip()
            ]
        text = _source_version_text(version)
        if not text.strip():
            return []
        paragraphs = [item.strip() for item in re.split(r"\n{2,}", text) if item.strip()]
        windows = []
        for idx, paragraph in enumerate(paragraphs[:max_windows]):
            windows.append(
                {
                    "locator_text": f"source window {idx + 1}",
                    "locator_type": EvidenceLocatorType.SOURCE_LEVEL,
                    "snippet_text": paragraph[:1600],
                    "channel": _retrieval_channel_for_version(version),
                }
            )
        return windows

    async def _chunks_for_source(self, source_id: str, *, limit: int) -> list[Chunk]:
        if not _uuid_or_none(source_id):
            return []
        result = await self.db.execute(
            select(Chunk)
            .where(Chunk.source_id == uuid.UUID(source_id))
            .order_by(Chunk.chunk_index.asc())
            .limit(max(1, limit))
        )
        return list(result.scalars())


async def build_source_inventory(db: AsyncSession, run_view: RunCorpusView) -> SourceMap:
    """Build neutral, deterministic source inventory for agent-led triage."""
    selected_source_ids = [uuid.UUID(source_id) for source_id in run_view.selected_source_ids if _uuid_or_none(source_id)]
    selected_version_ids = [uuid.UUID(version_id) for version_id in run_view.selected_source_version_ids if _uuid_or_none(version_id)]
    if not selected_source_ids:
        return SourceMap(gaps=["Run corpus view has no selected sources."])
    source_result = await db.execute(select(Source).where(Source.id.in_(selected_source_ids)))
    sources_by_id = {str(source.id): source for source in source_result.scalars()}
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.id.in_(selected_version_ids)))
    versions_by_source: dict[str, list[SourceVersion]] = {}
    for version in version_result.scalars():
        versions_by_source.setdefault(str(version.source_id), []).append(version)
    provider_result = await db.execute(select(ProviderCorpusFile).where(ProviderCorpusFile.source_version_id.in_(selected_version_ids)))
    provider_by_version = {str(item.source_version_id): item for item in provider_result.scalars()}

    cards: list[SourceCard] = []
    for source_id in run_view.selected_source_ids:
        source = sources_by_id.get(str(source_id))
        if source is None:
            continue
        versions = sorted(versions_by_source.get(str(source.id), []), key=lambda version: _readable_version_priority(version.kind))
        provider_statuses = [
            provider_by_version[str(version.id)].status
            for version in versions
            if str(version.id) in provider_by_version
        ]
        preview = _neutral_preview(source, versions)
        cards.append(
            SourceCard(
                source_id=str(source.id),
                source_version_ids=[str(version.id) for version in versions],
                evidence_ids=[],
                title=source.title or source.original_url or source.file_path or "Untitled source",
                source_type=source.source_type,
                url=source.original_url,
                domain=_source_domain(source.original_url),
                published_at=source.published_at.isoformat() if source.published_at else None,
                length_hint=_source_length_hint(source, versions),
                provider_status=_provider_status(provider_statuses),
                reliability=source.domain_reliability or "unknown",
                authority=source.authority_type or "unknown",
                summary=preview,
                useful_for=[],
                gaps_or_cautions=_inventory_cautions(source, versions, provider_statuses),
                representation_ids=[str(version.id) for version in versions],
                representation_summary=", ".join(
                    f"{version.kind}:{version.status}/{version.upload_status}" for version in versions[:8]
                ),
                ready_representation_count=sum(1 for version in versions if version.status == V2RecordStatus.READY),
                failed_representation_count=sum(1 for version in versions if version.status == V2RecordStatus.FAILED),
                coverage_status="ready" if any(_version_readable(version) for version in versions) else "partial",
            )
        )
    return SourceMap(
        source_cards=cards,
        gaps=[] if cards else ["No selected source could be loaded into inventory."],
        retrieval_notes=["Deterministic neutral inventory only; no editorial source-map search has been applied."],
    )


async def build_source_map_from_evidence(
    db: AsyncSession,
    run: GenerationRun,
    toolset: EvidenceToolset,
    *,
    query: str,
) -> SourceMap:
    source_results = await _sample_source_map_evidence(toolset, query=query, limit=30)
    evidence_by_source: dict[str, list[dict[str, Any]]] = {}
    for evidence in source_results.get("evidence", []):
        evidence_by_source.setdefault(str(evidence.get("source_id")), []).append(evidence)
    sources_result = await db.execute(select(Source).where(Source.id.in_([uuid.UUID(source_id) for source_id in toolset.run_view.selected_source_ids])))
    sources = list(sources_result.scalars())
    version_result = await db.execute(
        select(SourceVersion).where(SourceVersion.id.in_([uuid.UUID(version_id) for version_id in toolset.run_view.selected_source_version_ids]))
    )
    versions_by_source: dict[str, list[SourceVersion]] = {}
    for version in version_result.scalars():
        versions_by_source.setdefault(str(version.source_id), []).append(version)
    source_cards: list[SourceCard] = []
    quote_cards: list[QuoteCard] = []
    ranked: list[SourceTriageItem] = []
    for source in sources:
        evidence = evidence_by_source.get(str(source.id), [])
        versions = versions_by_source.get(str(source.id), [])
        evidence_ids = [str(item["id"]) for item in evidence[:6]]
        summary = evidence[0]["snippet_text"][:900] if evidence else (source.text_preview or source.title or source.original_url or "")
        source_cards.append(
            SourceCard(
                source_id=str(source.id),
                source_version_ids=[str(version.id) for version in versions],
                evidence_ids=evidence_ids,
                title=source.title or source.original_url or source.file_path or "Untitled source",
                source_type=source.source_type,
                url=source.original_url,
                reliability=source.domain_reliability or "unknown",
                authority=source.authority_type or "unknown",
                summary=summary or "No source preview available.",
                useful_for=_infer_useful_for(summary),
                gaps_or_cautions=_source_cautions(source, evidence),
                representation_ids=[str(version.id) for version in versions],
                representation_summary=f"{len(versions)} active source versions, {len(evidence_ids)} evidence anchors",
                ready_representation_count=len(versions),
                failed_representation_count=0,
                coverage_status="ready" if evidence_ids else "partial",
            )
        )
        ranked.append(
            SourceTriageItem(
                source_id=str(source.id),
                source_version_ids=[str(version.id) for version in versions],
                evidence_ids=evidence_ids,
                rank=0,
                score=_triage_score(source, evidence),
                authority=source.authority_type or "unknown",
                reliability=source.domain_reliability or "unknown",
                useful_for=_infer_useful_for(summary),
                top_quote_refs=evidence_ids[:4],
                relevance_reasons=["evidence-backed source map"] if evidence_ids else ["source available with weak evidence"],
                cautions=_source_cautions(source, evidence),
            )
        )
        for item in evidence[:3]:
            quote_cards.append(
                QuoteCard(
                    source_id=str(source.id),
                    source_version_id=str(item["source_version_id"]),
                    evidence_id=str(item["id"]),
                    quote=str(item["snippet_text"])[:500],
                    locator=str(item.get("locator_text") or ""),
                    narrative_use=str(item.get("purpose") or "source_map"),
                    representation_id=str(item["source_version_id"]),
                    source_kind=str(item.get("retrieval_channel") or "unknown"),
                    confidence=_confidence(item.get("score")),
                    confidence_reason="Selected from persisted EvidenceItem.",
                )
            )
    ranked.sort(key=lambda item: item.score, reverse=True)
    for idx, item in enumerate(ranked, start=1):
        item.rank = idx
    top_sources_by_function: dict[str, list[str]] = {}
    top_evidence_by_function: dict[str, list[str]] = {}
    for item in ranked:
        for function in item.useful_for or ["background"]:
            top_sources_by_function.setdefault(function, []).append(item.source_id)
            top_evidence_by_function.setdefault(function, []).extend(item.evidence_ids[:4])
    triage = SourceTriageReport(
        ranked_sources=ranked,
        top_sources_by_function={key: value[:8] for key, value in top_sources_by_function.items()},
        top_evidence_by_function={key: _dedupe(value)[:12] for key, value in top_evidence_by_function.items()},
        gaps=[f"No strong {label} source identified" for label in ("conflict", "mechanism", "consequence") if not top_sources_by_function.get(label)],
        contradictions=[f"{item.source_id}: low reliability source needs framing" for item in ranked if item.reliability == "low"],
    )
    return SourceMap(
        source_cards=source_cards,
        quote_cards=quote_cards,
        source_triage=triage,
        evidence_ids=[eid for card in source_cards for eid in card.evidence_ids],
        gaps=triage.gaps,
        contradictions=triage.contradictions,
    )


def materialize_source_map(
    source_map: SourceMap,
    *,
    inventory: SourceMap,
    evidence_records: list[dict[str, Any]],
) -> SourceMap:
    inventory_by_source = {card.source_id: card for card in inventory.source_cards}
    evidence_by_id = {str(item.get("id")): item for item in evidence_records if item.get("id")}
    evidence_ids = _source_map_evidence_ids(source_map)
    evidence_ids.extend(str(item.get("id")) for item in evidence_records if item.get("id"))
    evidence_ids = _dedupe([str(item) for item in evidence_ids if item])

    source_cards_by_id: dict[str, SourceCard] = {card.source_id: card for card in source_map.source_cards}
    for source_id in [*inventory_by_source.keys(), *[str(item.get("source_id")) for item in evidence_records if item.get("source_id")]]:
        if not source_id or source_id == "None":
            continue
        inventory_card = inventory_by_source.get(source_id)
        current = source_cards_by_id.get(source_id)
        if current is None and inventory_card is not None:
            current = inventory_card.model_copy(deep=True)
            source_cards_by_id[source_id] = current
        if current is None:
            continue
        if inventory_card is not None:
            current.source_version_ids = current.source_version_ids or inventory_card.source_version_ids
            current.title = current.title or inventory_card.title
            current.source_type = current.source_type or inventory_card.source_type
            current.url = current.url or inventory_card.url
            current.domain = current.domain or inventory_card.domain
            current.published_at = current.published_at or inventory_card.published_at
            current.length_hint = current.length_hint or inventory_card.length_hint
            current.provider_status = current.provider_status or inventory_card.provider_status
            current.representation_ids = current.representation_ids or inventory_card.representation_ids
            current.representation_summary = current.representation_summary or inventory_card.representation_summary
            current.ready_representation_count = current.ready_representation_count or inventory_card.ready_representation_count
            current.failed_representation_count = current.failed_representation_count or inventory_card.failed_representation_count
        current.evidence_ids = _dedupe(
            [
                *current.evidence_ids,
                *[
                    str(item.get("id"))
                    for item in evidence_records
                    if str(item.get("source_id")) == source_id and item.get("id")
                ],
            ]
        )
        if current.evidence_ids and current.coverage_status != "failed":
            current.coverage_status = "ready"

    quote_cards = list(source_map.quote_cards)
    quote_evidence_ids = {quote.evidence_id for quote in quote_cards if quote.evidence_id}
    for evidence_id in evidence_ids:
        if evidence_id in quote_evidence_ids:
            continue
        evidence = evidence_by_id.get(evidence_id)
        if not evidence:
            continue
        quote_cards.append(_quote_card_from_evidence(evidence))
        quote_evidence_ids.add(evidence_id)

    source_map.source_cards = list(source_cards_by_id.values())
    source_map.quote_cards = quote_cards
    source_map.evidence_ids = evidence_ids
    source_map.retrieval_notes = _dedupe(
        [
            *source_map.retrieval_notes,
            f"Materialized {len(evidence_ids)} persisted evidence item(s) after triage.",
        ]
    )
    return source_map


async def validate_source_map_gate(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    source_map: SourceMap,
    run_view: RunCorpusView,
) -> SourceTriageGate:
    selected_source_ids = {str(item) for item in run_view.selected_source_ids}
    selected_version_ids = {str(item) for item in run_view.selected_source_version_ids}
    evidence_result = await db.execute(select(EvidenceItem).where(EvidenceItem.generation_run_id == generation_run_id))
    evidence_by_id = {str(item.id): item for item in evidence_result.scalars()}
    trace_result = await db.execute(
        select(V2ToolTrace).where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_TRIAGE_READING_TOOL_NAMES)),
        )
    )
    tool_call_count = len(list(trace_result.scalars()))
    referenced_source_ids = _source_map_source_ids(source_map)
    referenced_version_ids = _source_map_version_ids(source_map)
    referenced_evidence_ids = set(_source_map_evidence_ids(source_map))
    invalid_source_ids = sorted(source_id for source_id in referenced_source_ids if source_id not in selected_source_ids)
    invalid_version_ids = sorted(version_id for version_id in referenced_version_ids if version_id not in selected_version_ids)
    invalid_evidence_ids = sorted(
        evidence_id
        for evidence_id in referenced_evidence_ids
        if evidence_id not in evidence_by_id
        or str(evidence_by_id[evidence_id].source_id) not in selected_source_ids
        or str(evidence_by_id[evidence_id].source_version_id) not in selected_version_ids
    )
    ungrounded_relationships = []
    for relation in source_map.source_triage.source_relationships:
        valid_relation_evidence = [eid for eid in relation.evidence_ids if eid in evidence_by_id]
        if relation.relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"} and not valid_relation_evidence:
            ungrounded_relationships.append(f"{relation.source_id}:{relation.relationship}:{relation.related_source_id}")
    for cluster in source_map.source_triage.source_clusters:
        if (cluster.primary_source_ids or cluster.secondary_source_ids or cluster.duplicate_source_ids) and not [
            eid for eid in cluster.evidence_ids if eid in evidence_by_id
        ]:
            ungrounded_relationships.append(f"cluster:{cluster.cluster_id}")
    missing_function_evidence = [
        function
        for function, evidence_ids in source_map.source_triage.top_evidence_by_function.items()
        if not [eid for eid in evidence_ids if eid in evidence_by_id and eid not in invalid_evidence_ids]
    ]
    if not source_map.source_triage.top_evidence_by_function:
        missing_function_evidence.append("top_evidence_by_function")
    warnings = []
    if not source_map.quote_cards:
        warnings.append("Triage produced no quote cards.")
    if not source_map.retrieval_notes:
        warnings.append("Triage produced no retrieval notes.")
    requires_tool_call = len(selected_source_ids) > 1
    evidence_count = len([eid for eid in referenced_evidence_ids if eid in evidence_by_id])
    passed = not (
        (requires_tool_call and tool_call_count == 0)
        or evidence_count == 0
        or invalid_source_ids
        or invalid_version_ids
        or invalid_evidence_ids
        or ungrounded_relationships
        or missing_function_evidence
    )
    summary = "Source triage gate passed." if passed else "Source triage gate failed."
    if requires_tool_call and tool_call_count == 0:
        summary += " No triage reading/search tool calls were recorded."
    if evidence_count == 0:
        summary += " No valid persisted evidence ids were referenced."
    return SourceTriageGate(
        passed=passed,
        summary=summary,
        tool_call_count=tool_call_count,
        checked_source_count=len(referenced_source_ids),
        checked_evidence_count=evidence_count,
        invalid_source_ids=invalid_source_ids,
        invalid_source_version_ids=invalid_version_ids,
        invalid_evidence_ids=invalid_evidence_ids,
        ungrounded_relationships=ungrounded_relationships,
        missing_function_evidence=missing_function_evidence,
        warnings=warnings,
    )


async def _sample_source_map_evidence(toolset: EvidenceToolset, *, query: str, limit: int) -> dict[str, Any]:
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for label, sample_query in _source_map_queries(query):
        remaining = max(1, limit - len(evidence_by_id))
        result = await toolset.search_evidence(
            query=sample_query,
            purpose=f"source_map:{label}",
            source_ids=[],
            limit=min(8, remaining),
        )
        for item in result.get("results", []):
            evidence_by_id.setdefault(str(item.get("id")), item)
        if len(evidence_by_id) >= limit:
            break
    evidence = list(evidence_by_id.values())[:limit]
    sources = await _source_summaries(toolset.db, toolset.run_view, evidence)
    return {"sources": sources, "evidence": evidence}


async def persist_evidence_item(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    source_version_id: uuid.UUID,
    provider: str | None = None,
    provider_vector_store_id: str | None = None,
    provider_file_id: str | None = None,
    provider_vector_store_file_id: str | None = None,
    response_id: str | None = None,
    tool_call_id: str | None = None,
    retrieval_channel: str,
    query_text: str | None = None,
    purpose: str | None = None,
    search_result_index: int | None = None,
    score: float | None = None,
    snippet_text: str = "",
    normalized_quote_text: str | None = None,
    locator_text: str | None = None,
    locator_type: str = EvidenceLocatorType.UNKNOWN,
    evidence_strength: str = EvidenceStrength.WEAK,
    metadata: dict[str, Any] | None = None,
) -> EvidenceItem:
    snippet = snippet_text.strip()
    snippet_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
    existing_result = await db.execute(
        select(EvidenceItem).where(
            EvidenceItem.generation_run_id == generation_run_id,
            EvidenceItem.source_version_id == source_version_id,
            EvidenceItem.snippet_hash == snippet_hash,
            EvidenceItem.retrieval_channel == str(retrieval_channel),
            EvidenceItem.locator_text == locator_text,
            EvidenceItem.query_text == query_text,
            EvidenceItem.purpose == purpose,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        existing.metadata_ = {
            **(existing.metadata_ or {}),
            "reused": True,
            "queries": sorted({*(existing.metadata_ or {}).get("queries", []), *([query_text] if query_text else [])}),
        }
        await db.flush()
        return existing
    item = EvidenceItem(
        generation_run_id=generation_run_id,
        project_id=project_id,
        source_id=source_id,
        source_version_id=source_version_id,
        provider=provider,
        provider_vector_store_id=provider_vector_store_id,
        provider_file_id=provider_file_id,
        provider_vector_store_file_id=provider_vector_store_file_id,
        response_id=response_id,
        tool_call_id=tool_call_id,
        retrieval_channel=str(retrieval_channel),
        query_text=query_text,
        purpose=purpose,
        search_result_index=search_result_index,
        score=score,
        snippet_text=snippet,
        normalized_quote_text=normalized_quote_text,
        locator_text=locator_text,
        locator_type=str(locator_type),
        evidence_strength=str(evidence_strength),
        snippet_hash=snippet_hash,
        metadata_=metadata or {},
    )
    db.add(item)
    await db.flush()
    return item


def evidence_payload(item: EvidenceItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "source_id": str(item.source_id),
        "source_version_id": str(item.source_version_id),
        "provider": item.provider,
        "provider_file_id": item.provider_file_id,
        "provider_vector_store_id": item.provider_vector_store_id,
        "provider_vector_store_file_id": item.provider_vector_store_file_id,
        "retrieval_channel": item.retrieval_channel,
        "query_text": item.query_text,
        "purpose": item.purpose,
        "score": item.score,
        "snippet_text": item.snippet_text,
        "normalized_quote_text": item.normalized_quote_text,
        "locator_text": item.locator_text,
        "locator_type": item.locator_type,
        "evidence_strength": item.evidence_strength,
        "snippet_hash": item.snippet_hash,
        "metadata": item.metadata_,
    }


async def _source_summaries(db: AsyncSession, run_view: RunCorpusView, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sources_result = await db.execute(select(Source).where(Source.id.in_([uuid.UUID(source_id) for source_id in run_view.selected_source_ids])))
    evidence_by_source: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        evidence_by_source.setdefault(str(item.get("source_id")), []).append(item)
    return [
        {
            "source_id": str(source.id),
            "title": source.title or source.original_url or source.file_path or "Untitled source",
            "source_type": source.source_type,
            "authority": source.authority_type,
            "reliability": source.domain_reliability,
            "evidence_ids": [item["id"] for item in evidence_by_source.get(str(source.id), [])[:6]],
        }
        for source in sources_result.scalars()
    ]


def _provider_filters(source_ids: list[str]) -> dict[str, Any]:
    valid = [source_id for source_id in source_ids if _uuid_or_none(source_id)]
    if not valid:
        return {"type": "eq", "key": "is_active", "value": True}
    source_filters = [{"type": "eq", "key": "source_id", "value": source_id} for source_id in valid]
    return {
        "type": "and",
        "filters": [
            {"type": "eq", "key": "is_active", "value": True},
            {"type": "or", "filters": source_filters} if len(source_filters) > 1 else source_filters[0],
        ],
    }


def _provider_snippet(item: Any) -> str:
    content = getattr(item, "content", []) or []
    return "\n\n".join(str(_part_text(part) or "") for part in content if _part_text(part)).strip()


def _part_text(part: Any) -> str | None:
    if isinstance(part, dict):
        return part.get("text")
    return getattr(part, "text", None)


def _source_version_text(version: SourceVersion) -> str:
    if not version.storage_path or version.kind in {"original_url", "original_file"}:
        return ""
    try:
        return download_text(get_minio_client(), version.storage_path)
    except Exception:
        return ""


def _query_tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[^\W_]{3,}", (text or "").lower(), flags=re.UNICODE) if token]


def _source_map_queries(query: str) -> list[tuple[str, str]]:
    base = (query or "").strip() or "project source overview"
    return [
        ("overview", base),
        ("conflict", f"{base} conflict controversy disagreement stakes"),
        ("mechanism", f"{base} mechanism process system incentives"),
        ("history", f"{base} history timeline origin turning point"),
        ("consequence", f"{base} consequence impact cost result"),
        ("human_cost", f"{base} people workers community human cost"),
        ("exception", f"{base} exception counterexample uncertainty gap"),
    ]


def _source_map_evidence_ids(source_map: SourceMap) -> list[str]:
    ids = [*source_map.evidence_ids]
    ids.extend(eid for card in source_map.source_cards for eid in card.evidence_ids)
    ids.extend(quote.evidence_id for quote in source_map.quote_cards if quote.evidence_id)
    ids.extend(eid for item in source_map.source_triage.ranked_sources for eid in item.evidence_ids)
    ids.extend(eid for values in source_map.source_triage.top_evidence_by_function.values() for eid in values)
    ids.extend(eid for cluster in source_map.source_triage.source_clusters for eid in cluster.evidence_ids)
    ids.extend(eid for relation in source_map.source_triage.source_relationships for eid in relation.evidence_ids)
    return _dedupe([str(item) for item in ids if item])


def _source_map_source_ids(source_map: SourceMap) -> set[str]:
    ids = {card.source_id for card in source_map.source_cards}
    ids.update(quote.source_id for quote in source_map.quote_cards)
    ids.update(item.source_id for item in source_map.source_triage.ranked_sources)
    ids.update(source_id for values in source_map.source_triage.top_sources_by_function.values() for source_id in values)
    for cluster in source_map.source_triage.source_clusters:
        ids.update(cluster.source_ids)
        ids.update(cluster.primary_source_ids)
        ids.update(cluster.secondary_source_ids)
        ids.update(cluster.duplicate_source_ids)
    for relation in source_map.source_triage.source_relationships:
        ids.add(relation.source_id)
        ids.add(relation.related_source_id)
    return {str(item) for item in ids if item}


def _source_map_version_ids(source_map: SourceMap) -> set[str]:
    ids = {version_id for card in source_map.source_cards for version_id in card.source_version_ids}
    ids.update(quote.source_version_id for quote in source_map.quote_cards if quote.source_version_id)
    ids.update(version_id for item in source_map.source_triage.ranked_sources for version_id in item.source_version_ids)
    return {str(item) for item in ids if item}


def _quote_card_from_evidence(evidence: dict[str, Any]) -> QuoteCard:
    return QuoteCard(
        source_id=str(evidence.get("source_id")),
        source_version_id=str(evidence.get("source_version_id")),
        evidence_id=str(evidence.get("id")),
        quote=str(evidence.get("snippet_text") or "")[:500],
        locator=str(evidence.get("locator_text") or ""),
        narrative_use=str(evidence.get("purpose") or "triage"),
        representation_id=str(evidence.get("source_version_id")),
        source_kind=str(evidence.get("retrieval_channel") or "unknown"),
        confidence=_confidence(evidence.get("score")),
        confidence_reason="Materialized from persisted EvidenceItem after triage.",
    )


def _uuid_list(values: list[str]) -> list[uuid.UUID]:
    return [uuid.UUID(str(value)) for value in values if _uuid_or_none(str(value))]


def _readable_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.PAGE_MARKED_TEXT: 0,
        SourceRepresentationKind.EXTRACTED_TEXT: 1,
        SourceRepresentationKind.TRANSCRIPT: 1,
        SourceRepresentationKind.RENDERED_SNAPSHOT: 2,
        SourceRepresentationKind.FETCHED_HTML: 3,
        SourceRepresentationKind.ORIGINAL_FILE: 10,
        SourceRepresentationKind.ORIGINAL_URL: 100,
    }
    return order.get(kind, 50)


def _retrieval_channel_for_version(version: SourceVersion) -> str:
    if version.kind == SourceRepresentationKind.TRANSCRIPT:
        return EvidenceRetrievalChannel.LOCAL_TRANSCRIPT_SEARCH
    if version.kind in {SourceRepresentationKind.FETCHED_HTML, SourceRepresentationKind.RENDERED_SNAPSHOT}:
        return EvidenceRetrievalChannel.WEB_SNAPSHOT
    return EvidenceRetrievalChannel.LOCAL_CHUNK_SEARCH


def _window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str]:
    normalized = text.strip()
    if not normalized:
        return "", locator or "source window"
    if locator:
        idx = normalized.lower().find(locator.lower())
        if idx >= 0:
            start = max(0, idx - max_chars // 4)
            end = min(len(normalized), start + max_chars)
            return normalized[start:end], locator
    return normalized[:max_chars], locator or "source beginning"


def _select_chunk_window(chunks: list[Chunk], *, locator: str) -> Chunk:
    if locator:
        lower = locator.lower()
        for chunk in chunks:
            heading = " > ".join(chunk.heading_path or [])
            if lower in heading.lower() or lower in chunk.content.lower():
                return chunk
    return chunks[0]


def _chunk_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.EXTRACTED_TEXT: 0,
        SourceRepresentationKind.TRANSCRIPT: 0,
    }
    return order.get(kind, 100)


def _source_domain(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    return parsed.netloc or None


def _source_length_hint(source: Source, versions: list[SourceVersion]) -> str:
    metadata = source.metadata_ or {}
    for key in ("char_count", "token_count", "page_count", "duration_seconds"):
        if metadata.get(key) is not None:
            return f"{key}={metadata[key]}"
    if source.text_preview:
        return f"preview_chars={len(source.text_preview)}"
    readable = [version for version in versions if _version_readable(version)]
    return f"readable_versions={len(readable)}"


def _provider_status(statuses: list[str]) -> str:
    if not statuses:
        return "not_uploaded"
    if any(status == V2RecordStatus.READY for status in statuses):
        return "ready"
    if any(status == V2RecordStatus.PENDING for status in statuses):
        return "pending"
    if all(status == V2RecordStatus.FAILED for status in statuses):
        return "failed"
    return "mixed"


def _neutral_preview(source: Source, versions: list[SourceVersion]) -> str:
    preview = (source.text_preview or "").strip()
    if preview:
        return preview[:700]
    version_labels = ", ".join(f"{version.kind}:{version.status}" for version in versions[:6])
    return f"Neutral source metadata only. Available versions: {version_labels or 'none'}."


def _inventory_cautions(source: Source, versions: list[SourceVersion], provider_statuses: list[str]) -> list[str]:
    cautions: list[str] = []
    if source.domain_reliability in {"low", "unknown"}:
        cautions.append(f"Reliability hint is {source.domain_reliability}.")
    if source.authority_type == "unknown":
        cautions.append("Authority type is unknown.")
    if source.source_type == "web" and not source.published_at:
        cautions.append("Publication date is missing.")
    if not any(_version_readable(version) for version in versions):
        cautions.append("No local readable snapshot/text version is available; provider search may be required.")
    if provider_statuses and not any(status == V2RecordStatus.READY for status in provider_statuses):
        cautions.append("Provider index is not ready for this source.")
    return cautions


def _version_readable(version: SourceVersion) -> bool:
    return bool(version.status == V2RecordStatus.READY and version.kind != SourceRepresentationKind.ORIGINAL_URL)


def _infer_useful_for(text: str) -> list[str]:
    lower = (text or "").lower()
    buckets: list[str] = []
    for label, markers in {
        "history": ("history", "timeline", "founded", "began", "started"),
        "mechanism": ("mechanism", "because", "how it works", "process", "system"),
        "conflict": ("conflict", "debate", "critic", "lawsuit", "controvers"),
        "consequence": ("impact", "result", "consequence", "cost", "effect"),
        "human_cost": ("human", "people", "worker", "community"),
    }.items():
        if any(marker in lower for marker in markers):
            buckets.append(label)
    return buckets or ["background"]


def _source_cautions(source: Source, evidence: list[dict[str, Any]]) -> list[str]:
    cautions: list[str] = []
    if not evidence:
        cautions.append("No persisted evidence snippet was found during source-map search.")
    if source.domain_reliability in {"low", "unknown"}:
        cautions.append(f"Reliability is {source.domain_reliability}.")
    if source.source_type == "web" and not source.published_at:
        cautions.append("Published date is missing.")
    return cautions


def _triage_score(source: Source, evidence: list[dict[str, Any]]) -> float:
    score = 0.0
    score += {"primary": 3.0, "secondary": 2.0, "tertiary": 0.8, "unknown": 0.5}.get(source.authority_type, 0.5)
    score += {"high": 2.0, "medium": 1.0, "unknown": 0.0, "low": -1.0}.get(source.domain_reliability, 0.0)
    score += min(3.0, len(evidence) * 0.5)
    score += sum(float(item.get("score") or 0.0) for item in evidence[:4]) * 0.05
    return round(score, 3)


def _confidence(score: Any) -> float:
    if score is None:
        return 0.7
    try:
        return max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        return 0.7


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()
