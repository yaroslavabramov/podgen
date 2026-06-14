from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import QuoteCard, SourceCard, SourceMap, SourceTriageItem, SourceTriageReport
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
    V2RecordStatus,
)
from app.models.source import Source
from app.services.llm import get_openai_client
from app.services.storage import download_text, get_minio_client


_CHUNK_BACKED_VERSION_KINDS = (
    SourceRepresentationKind.EXTRACTED_TEXT,
    SourceRepresentationKind.TRANSCRIPT,
)


@dataclass
class EvidenceToolset:
    db: AsyncSession
    run: GenerationRun
    run_view: RunCorpusView
    provider_index: ProviderCorpusIndex | None

    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                name="search_evidence",
                description="Search the selected corpus and persist returned evidence. Returns saved evidence ids plus snippets.",
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
                description="Read a saved evidence item by id.",
                parameters={
                    "type": "object",
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
                handler=self.read_evidence_tool,
            ),
            AgentTool(
                name="find_quotes",
                description="Find quote-like evidence candidates and persist them as evidence items.",
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
                description="Search for source-level candidates and evidence-backed snippets for corpus triage.",
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
        ]

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
    for item in ranked:
        for function in item.useful_for or ["background"]:
            top_sources_by_function.setdefault(function, []).append(item.source_id)
    triage = SourceTriageReport(
        ranked_sources=ranked,
        top_sources_by_function={key: value[:8] for key, value in top_sources_by_function.items()},
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


def _uuid_list(values: list[str]) -> list[uuid.UUID]:
    return [uuid.UUID(str(value)) for value in values if _uuid_or_none(str(value))]


def _chunk_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.EXTRACTED_TEXT: 0,
        SourceRepresentationKind.TRANSCRIPT: 0,
    }
    return order.get(kind, 100)


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


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()
