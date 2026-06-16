from __future__ import annotations

import hashlib
import io
import json
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
from app.generation_v2.schemas import (
    QuoteCard,
    SourceCard,
    SourceMap,
    SourceRelationship,
    SourceTriageGate,
    SourceTriageItem,
    SourceTriageReport,
)
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
    "list_source_versions",
    "search_sources",
    "search_evidence",
    "find_quotes",
    "read_evidence",
    "read_source",
    "read_source_window",
    "read_source_with_model",
    "skim_source",
    "inspect_source",
    "search_within_source",
    "compare_sources",
    "select_evidence",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_gap",
    "finalize_source_map",
    "openai.file_search.observed",
}
_WRITE_TOOL_NAMES = {
    "select_evidence",
    "upsert_source_card",
    "create_quote_card",
    "record_source_relationship",
    "record_gap",
    "finalize_source_map",
}
EVIDENCE_STATUS_OBSERVED = "observed"
EVIDENCE_STATUS_SELECTED = "selected"
EVIDENCE_STATUS_CITED = "cited"
EVIDENCE_STATUS_REJECTED = "rejected"


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
                name="list_source_versions",
                description=(
                    "List available source versions/representations for one source: original files, page-marked text, rendered/article text, transcript, provider file status, and local readability. "
                    "Use this before inspect_source or read_source_window when you need the best representation for exact reading."
                ),
                parameters={
                    "type": "object",
                    "properties": {"source_id": {"type": "string"}},
                    "required": ["source_id"],
                },
                handler=self.list_source_versions_tool,
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
                name="inspect_source",
                description=(
                    "Inspect a source/source version structure: headings, chunk labels, page/window hints, timestamp/section-like locators, and available representations. "
                    "This is for orientation before exact reads; it does not select evidence for the SourceMap."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "max_items": {"type": "integer", "minimum": 1, "maximum": 80},
                    },
                    "required": ["source_id", "source_version_id", "max_items"],
                },
                handler=self.inspect_source_tool,
            ),
            AgentTool(
                name="read_source",
                description=(
                    "Read a concrete window from a selected source/source version and persist that window as an EvidenceItem. "
                    "Use source_id when you want the best readable version, or source_version_id when you need a specific version. "
                    "Strict schema requires both source_id and source_version_id; put an empty string in the one you are not using. "
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
                name="read_source_window",
                description=(
                    "Fallback/debug reader for local text snapshots, HTML, transcripts, and chunk windows. "
                    "Open an exact page, page range, heading, timestamp, section, beginning, or around-phrase window when local text is available. "
                    "For complex PDFs/documents, prefer read_source_with_model because it lets OpenAI read the uploaded file directly. "
                    "The returned window is persisted as observed EvidenceItem; call select_evidence if you want it in the SourceMap."
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
                name="read_source_with_model",
                description=(
                    "Deep-read an uploaded source file with OpenAI input_file and persist the model-read result as an EvidenceItem. "
                    "Use this after hosted file_search identifies an important PDF/document/source, or when citation support requires reading the actual file rather than local parser chunks. "
                    "Provide source_id or source_version_id, a focused question/claim, and optional locator_hint. "
                    "Strict schema requires source_id, source_version_id, focus_question, claim_text, purpose, and locator_hint; put an empty string in unused optional fields or in the unused id field. "
                    "The backend selects the best provider-backed source version, asks the model for strongest support, quote/snippet, support_summary, uncertainty, and optional locator, then returns an evidence_id. "
                    "If provider input_file reading is unavailable, the tool may return retrieval_channel=model_file_read_local_fallback; treat that channel as a risk/coverage signal and reflect it in confidence/support instead of pretending it was a direct file read. "
                    "Call select_evidence if the result should enter SourceMap/research/citations. Page numbers are useful when reported, but not required."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "focus_question": {"type": "string"},
                        "claim_text": {"type": "string"},
                        "purpose": {"type": "string"},
                        "locator_hint": {"type": "string"},
                    },
                    "required": ["source_id", "source_version_id", "focus_question", "claim_text", "purpose", "locator_hint"],
                },
                handler=self.read_source_with_model_tool,
            ),
            AgentTool(
                name="search_within_source",
                description=(
                    "Search for a phrase/topic inside exactly one selected source, persist matching windows as observed EvidenceItems, and return evidence ids. "
                    "Use this for targeted follow-up after file_search finds a promising source."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                    },
                    "required": ["source_id", "source_version_id", "query", "limit"],
                },
                handler=self.search_within_source_tool,
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
            AgentTool(
                name="select_evidence",
                description=(
                    "Write action: explicitly select an observed EvidenceItem as working support for the SourceMap, research, quote, relationship, gap, or citation. "
                    "Only selected evidence can enter SourceMap. Provide the evidence_id exactly as returned by file_search/read/search tools."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "purpose": {"type": "string"},
                        "role": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["evidence_id", "purpose", "role", "rationale"],
                },
                handler=self.select_evidence_tool,
            ),
            AgentTool(
                name="upsert_source_card",
                description=(
                    "Write action: create/update a SourceCard from selected evidence. evidence_ids must already be selected with select_evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "useful_for": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "gaps_or_cautions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["source_id", "summary", "useful_for", "evidence_ids", "gaps_or_cautions"],
                },
                handler=self.upsert_source_card_tool,
            ),
            AgentTool(
                name="create_quote_card",
                description=(
                    "Write action: create a QuoteCard from selected evidence. quote text must come from the selected EvidenceItem snippet/window."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "quote": {"type": "string"},
                        "locator": {"type": "string"},
                        "narrative_use": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "confidence_reason": {"type": "string"},
                    },
                    "required": [
                        "evidence_id",
                        "source_id",
                        "source_version_id",
                        "quote",
                        "locator",
                        "narrative_use",
                        "confidence",
                        "confidence_reason",
                    ],
                },
                handler=self.create_quote_card_tool,
            ),
            AgentTool(
                name="record_source_relationship",
                description=(
                    "Write action: record duplicate, near_duplicate, primary_to_secondary, contradicts, updates, or background_for relationships using selected evidence ids."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "related_source_id": {"type": "string"},
                        "relationship": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["source_id", "related_source_id", "relationship", "evidence_ids", "rationale"],
                },
                handler=self.record_source_relationship_tool,
            ),
            AgentTool(
                name="record_gap",
                description=(
                    "Write action: record a gap, contradiction, or caution discovered during triage/research, with selected evidence ids where applicable."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "description": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["kind", "description", "source_ids", "evidence_ids"],
                },
                handler=self.record_gap_tool,
            ),
            AgentTool(
                name="finalize_source_map",
                description=(
                    "Write action: signal that triage write actions are complete and SourceMap should be assembled by the backend from selected evidence and write actions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "ready": {"type": "boolean"},
                    },
                    "required": ["summary", "ready"],
                },
                handler=self.finalize_source_map_tool,
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

    async def _resolve_local_fallback_source_version(self, *, source_id: str, source_version_id: str) -> SourceVersion | None:
        selected_version_ids = set(str(item) for item in self.run_view.selected_source_version_ids)
        selected_source_ids = set(str(item) for item in self.run_view.selected_source_ids)
        target_source_id = source_id
        if source_version_id and _uuid_or_none(source_version_id):
            version = await self.db.get(SourceVersion, uuid.UUID(source_version_id))
            if version is None or str(version.id) not in selected_version_ids or str(version.source_id) not in selected_source_ids:
                return None
            if _version_can_back_local_window(version):
                return version
            target_source_id = str(version.source_id)
        if not target_source_id or not _uuid_or_none(target_source_id) or target_source_id not in selected_source_ids:
            return None
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.source_id == uuid.UUID(target_source_id),
                SourceVersion.id.in_(_uuid_list(list(selected_version_ids))),
                SourceVersion.status == V2RecordStatus.READY,
                SourceVersion.is_active.is_(True),
            )
        )
        versions = [version for version in result.scalars() if _version_can_back_local_window(version)]
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
            snippet, locator_text, locator_type = _window_for_locator(text, locator=locator, max_chars=max_chars)
            return (
                snippet,
                locator_text,
                locator_type,
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
        outline = _source_outline(text, max_items=max_windows)
        if outline:
            windows = []
            for item in outline[:max_windows]:
                snippet, locator_text, locator_type = _window_for_locator(text, locator=item["locator"], max_chars=1600)
                if snippet.strip():
                    windows.append(
                        {
                            "locator_text": locator_text,
                            "locator_type": locator_type,
                            "snippet_text": snippet,
                            "channel": _retrieval_channel_for_version(version),
                        }
                    )
            if windows:
                return windows
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

    async def _resolve_provider_file_candidates_for_model_read(
        self,
        *,
        source_id: str,
        source_version_id: str,
    ) -> list[tuple[ProviderCorpusFile, SourceVersion]]:
        if self.provider_index is None:
            return []
        selected_source_ids = {str(item) for item in self.run_view.selected_source_ids}
        selected_version_ids = {str(item) for item in self.run_view.selected_source_version_ids}
        target_source_id = ""
        if source_version_id and _uuid_or_none(source_version_id):
            if source_version_id not in selected_version_ids:
                return []
            version = await self.db.get(SourceVersion, uuid.UUID(source_version_id))
            if version is None or str(version.source_id) not in selected_source_ids:
                return []
            target_source_id = str(version.source_id)
        elif source_id and _uuid_or_none(source_id):
            if source_id not in selected_source_ids:
                return []
            target_source_id = source_id
        else:
            return []
        filters = [
            ProviderCorpusFile.provider_index_id == self.provider_index.id,
            ProviderCorpusFile.status == V2RecordStatus.READY,
            ProviderCorpusFile.provider_file_id.is_not(None),
            ProviderCorpusFile.source_version_id.in_(_uuid_list(list(selected_version_ids))),
            ProviderCorpusFile.source_id == uuid.UUID(target_source_id),
        ]
        result = await self.db.execute(select(ProviderCorpusFile).where(*filters))
        candidates: list[tuple[ProviderCorpusFile, SourceVersion]] = []
        for provider_file in result.scalars():
            version = await self.db.get(SourceVersion, provider_file.source_version_id)
            if not version or not self._in_run_corpus_view(provider_file.source_id, provider_file.source_version_id):
                continue
            candidates.append((provider_file, version))
        candidates.sort(key=lambda item: _model_read_version_priority(item[1].kind))
        return candidates

    async def _save_model_read_failure_trace(
        self,
        *,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
        purpose: str,
        focus_question: str,
        claim_text: str,
        locator_hint: str,
        error: str,
    ) -> None:
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="openai.responses.create.input_file",
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "provider_file_id": provider_file.provider_file_id,
                "purpose": purpose,
                "focus_question": focus_question,
                "claim_text": claim_text,
                "locator_hint": locator_hint,
            },
            output_json={"error": error},
        )

    async def _model_read_provider_file(
        self,
        *,
        provider_file: ProviderCorpusFile,
        version: SourceVersion,
        focus_question: str,
        claim_text: str,
        purpose: str,
        locator_hint: str,
    ) -> tuple[dict[str, Any], str | None]:
        if not provider_file.provider_file_id:
            raise RuntimeError("Provider file id is missing")
        client = get_openai_client()
        input_file_id = await self._ensure_model_read_file_id(provider_file, version)
        prompt = _model_file_read_prompt(
            focus_question=focus_question,
            claim_text=claim_text,
            purpose=purpose,
            locator_hint=locator_hint,
            source_id=str(version.source_id),
            source_version_id=str(version.id),
            source_kind=str(version.kind),
        )
        response = await client.responses.create(
            model=settings.generation_v2_openai_model,
            instructions=(
                "You are a careful source reader. Use only the attached input_file. "
                "Return JSON only. Do not rely on memory or outside knowledge."
            ),
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_file", "file_id": input_file_id},
                    ],
                }
            ],
            max_output_tokens=3000,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "ModelFileReadResult",
                    "schema": _MODEL_FILE_READ_SCHEMA,
                    "strict": True,
                }
            },
        )
        output_text = getattr(response, "output_text", "") or ""
        read_result = _parse_model_file_read_result(output_text)
        read_result["_model_read_file_id"] = input_file_id
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="openai.responses.create.input_file",
            input_json={
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "provider_file_id": provider_file.provider_file_id,
                "model_read_file_id": input_file_id,
                "source_kind": str(version.kind),
                "purpose": purpose,
                "focus_question": focus_question,
                "claim_text": claim_text,
                "locator_hint": locator_hint,
            },
            output_json={
                "response_id": getattr(response, "id", None),
                "supports_query": read_result.get("supports_query"),
                "support_level": read_result.get("support_level"),
                "confidence": read_result.get("confidence"),
            },
            provider_call_id=getattr(response, "id", None),
        )
        return read_result, getattr(response, "id", None)

    async def _ensure_model_read_file_id(self, provider_file: ProviderCorpusFile, version: SourceVersion) -> str:
        attributes = provider_file.attributes or {}
        model_read_file_id = str(attributes.get("model_read_file_id") or "")
        if model_read_file_id:
            return model_read_file_id
        filename, content = _model_read_upload_payload(version)
        if not content:
            raise RuntimeError("Source version content is empty; cannot upload model-read input file")
        client = get_openai_client()
        file_obj = await client.files.create(file=(filename, io.BytesIO(content)), purpose="user_data")
        model_read_file_id = str(file_obj.id)
        provider_file.attributes = {
            **attributes,
            "model_read_file_id": model_read_file_id,
            "model_read_file_purpose": "user_data",
            "model_read_filename": filename,
            "vector_store_provider_file_id": provider_file.provider_file_id,
        }
        await self.db.flush()
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            tool_name="openai.files.create.model_read",
            input_json={"source_version_id": str(version.id), "filename": filename, "purpose": "user_data"},
            output_json={"model_read_file_id": model_read_file_id},
            provider_call_id=model_read_file_id,
        )
        return model_read_file_id


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


async def build_source_map_from_write_actions(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    inventory: SourceMap,
) -> SourceMap:
    trace_result = await db.execute(
        select(V2ToolTrace)
        .where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_WRITE_TOOL_NAMES)),
        )
        .order_by(V2ToolTrace.created_at.asc())
    )
    traces = [trace for trace in trace_result.scalars() if _trace_accepted(trace)]
    evidence_result = await db.execute(select(EvidenceItem).where(EvidenceItem.generation_run_id == generation_run_id))
    evidence_by_id = {str(item.id): item for item in evidence_result.scalars()}
    inventory_by_source = {card.source_id: card for card in inventory.source_cards}
    selected_evidence_ids = _selected_evidence_ids_from_actions(traces, evidence_by_id)
    selected_evidence_by_id = {
        evidence_id: evidence_by_id[evidence_id]
        for evidence_id in selected_evidence_ids
        if evidence_id in evidence_by_id
    }

    source_cards_by_id: dict[str, SourceCard] = {}
    quote_cards: list[QuoteCard] = []
    relationships: list[SourceRelationship] = []
    gaps: list[str] = []
    contradictions: list[str] = []
    retrieval_notes: list[str] = []
    finalized = False

    for trace in traces:
        action = trace.input_json or {}
        if trace.tool_name == "upsert_source_card":
            source_id = str(action.get("source_id") or "")
            base = inventory_by_source.get(source_id)
            if base is None:
                continue
            evidence_ids = _dedupe([str(eid) for eid in action.get("evidence_ids") or [] if str(eid) in selected_evidence_by_id])
            card = base.model_copy(deep=True)
            card.summary = str(action.get("summary") or base.summary)
            card.useful_for = [str(item) for item in action.get("useful_for") or [] if item]
            card.evidence_ids = evidence_ids
            card.gaps_or_cautions = [str(item) for item in action.get("gaps_or_cautions") or [] if item]
            card.coverage_status = "ready" if evidence_ids else card.coverage_status
            source_cards_by_id[source_id] = card
        elif trace.tool_name == "create_quote_card":
            evidence_id = str(action.get("evidence_id") or "")
            if evidence_id not in selected_evidence_by_id:
                continue
            evidence = selected_evidence_by_id[evidence_id]
            quote_cards.append(
                QuoteCard(
                    source_id=str(action.get("source_id") or evidence.source_id),
                    source_version_id=str(action.get("source_version_id") or evidence.source_version_id),
                    evidence_id=evidence_id,
                    quote=str(action.get("quote") or evidence.snippet_text[:500]),
                    locator=str(action.get("locator") or evidence.locator_text or ""),
                    narrative_use=str(action.get("narrative_use") or "triage"),
                    representation_id=str(evidence.source_version_id),
                    source_kind=evidence.retrieval_channel,
                    confidence=_confidence(action.get("confidence")),
                    confidence_reason=str(action.get("confidence_reason") or "Created from selected evidence."),
                )
            )
        elif trace.tool_name == "record_source_relationship":
            relationships.append(
                SourceRelationship(
                    source_id=str(action.get("source_id") or ""),
                    related_source_id=str(action.get("related_source_id") or ""),
                    relationship=_relationship_kind(str(action.get("relationship") or "")),
                    evidence_ids=[str(eid) for eid in action.get("evidence_ids") or [] if str(eid) in selected_evidence_by_id],
                    rationale=str(action.get("rationale") or ""),
                )
            )
        elif trace.tool_name == "record_gap":
            description = str(action.get("description") or "")
            if not description:
                continue
            if str(action.get("kind") or "") == "contradiction":
                contradictions.append(description)
            else:
                gaps.append(description)
        elif trace.tool_name == "finalize_source_map":
            finalized = bool(action.get("ready"))
            retrieval_notes.append(str(action.get("summary") or "Source map finalized."))

    for evidence_id, evidence in selected_evidence_by_id.items():
        source_id = str(evidence.source_id)
        base = inventory_by_source.get(source_id)
        if base is None:
            continue
        card = source_cards_by_id.get(source_id) or base.model_copy(deep=True)
        if evidence_id not in card.evidence_ids:
            card.evidence_ids.append(evidence_id)
        if not card.useful_for:
            card.useful_for = [_selected_role(evidence)]
        if card.coverage_status != "failed":
            card.coverage_status = "ready"
        source_cards_by_id[source_id] = card

    ranked = []
    for idx, card in enumerate(
        sorted(source_cards_by_id.values(), key=lambda item: (-len(item.evidence_ids), item.title)),
        start=1,
    ):
        ranked.append(
            SourceTriageItem(
                source_id=card.source_id,
                source_version_ids=card.source_version_ids,
                evidence_ids=card.evidence_ids,
                rank=idx,
                score=float(max(0, len(card.evidence_ids))),
                authority=card.authority,
                reliability=card.reliability,
                useful_for=card.useful_for,
                top_quote_refs=[quote.evidence_id or "" for quote in quote_cards if quote.source_id == card.source_id][:6],
                relevance_reasons=["Selected by source triage write actions."],
                cautions=card.gaps_or_cautions,
            )
        )
    top_sources_by_function: dict[str, list[str]] = {}
    top_evidence_by_function: dict[str, list[str]] = {}
    for item in ranked:
        for function in item.useful_for or ["background"]:
            top_sources_by_function.setdefault(function, []).append(item.source_id)
            top_evidence_by_function.setdefault(function, []).extend(item.evidence_ids)
    if not finalized:
        gaps.append("Triage did not call finalize_source_map.")

    return SourceMap(
        source_cards=list(source_cards_by_id.values()),
        quote_cards=quote_cards,
        source_triage=SourceTriageReport(
            ranked_sources=ranked,
            top_sources_by_function={key: _dedupe(value)[:10] for key, value in top_sources_by_function.items()},
            top_evidence_by_function={key: _dedupe(value)[:12] for key, value in top_evidence_by_function.items()},
            gaps=gaps,
            contradictions=contradictions,
            source_relationships=relationships,
        ),
        evidence_ids=list(selected_evidence_by_id.keys()),
        gaps=gaps,
        contradictions=contradictions,
        retrieval_notes=retrieval_notes,
    )


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
    selected_evidence_ids = {
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if (evidence.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
    }
    trace_result = await db.execute(
        select(V2ToolTrace).where(
            V2ToolTrace.generation_run_id == generation_run_id,
            V2ToolTrace.phase == "v2_source_triage",
            V2ToolTrace.tool_name.in_(list(_TRIAGE_READING_TOOL_NAMES)),
        )
    )
    triage_traces = list(trace_result.scalars())
    tool_call_count = len(triage_traces)
    referenced_source_ids = _source_map_source_ids(source_map)
    referenced_version_ids = _source_map_version_ids(source_map)
    referenced_evidence_ids = set(_source_map_evidence_ids(source_map))
    invalid_source_ids = sorted(source_id for source_id in referenced_source_ids if source_id not in selected_source_ids)
    invalid_version_ids = sorted(version_id for version_id in referenced_version_ids if version_id not in selected_version_ids)
    invalid_evidence_ids = sorted(
        evidence_id
        for evidence_id in referenced_evidence_ids
        if evidence_id not in evidence_by_id
        or evidence_id not in selected_evidence_ids
        or str(evidence_by_id[evidence_id].source_id) not in selected_source_ids
        or str(evidence_by_id[evidence_id].source_version_id) not in selected_version_ids
    )
    ungrounded_relationships = []
    for relation in source_map.source_triage.source_relationships:
        valid_relation_evidence = [eid for eid in relation.evidence_ids if eid in selected_evidence_ids]
        if relation.relationship in {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates"} and not valid_relation_evidence:
            ungrounded_relationships.append(f"{relation.source_id}:{relation.relationship}:{relation.related_source_id}")
    for cluster in source_map.source_triage.source_clusters:
        if (cluster.primary_source_ids or cluster.secondary_source_ids or cluster.duplicate_source_ids) and not [
            eid for eid in cluster.evidence_ids if eid in selected_evidence_ids
        ]:
            ungrounded_relationships.append(f"cluster:{cluster.cluster_id}")
    missing_function_evidence = [
        function
        for function, evidence_ids in source_map.source_triage.top_evidence_by_function.items()
        if not [eid for eid in evidence_ids if eid in selected_evidence_ids and eid not in invalid_evidence_ids]
    ]
    if not source_map.source_triage.top_evidence_by_function:
        missing_function_evidence.append("top_evidence_by_function")
    warnings = []
    if not source_map.quote_cards:
        warnings.append("Triage produced no quote cards.")
    if not source_map.retrieval_notes:
        warnings.append("Triage produced no retrieval notes.")
    requires_tool_call = len(selected_source_ids) > 1
    unselected_quote_evidence = [
        str(quote.evidence_id)
        for quote in source_map.quote_cards
        if quote.evidence_id and quote.evidence_id not in selected_evidence_ids
    ]
    if unselected_quote_evidence:
        invalid_evidence_ids = sorted({*invalid_evidence_ids, *unselected_quote_evidence})
    evidence_count = len([eid for eid in referenced_evidence_ids if eid in selected_evidence_ids])
    missing_source_card_evidence_source_ids = _missing_source_card_evidence_sources(
        source_map,
        evidence_by_id,
        selected_evidence_ids,
        invalid_evidence_ids=set(invalid_evidence_ids),
    )
    missing_model_read_source_ids = _missing_model_read_sources(source_map, evidence_by_id, selected_evidence_ids)
    if missing_model_read_source_ids:
        warnings.append(
            "Some PDF/document source cards lack direct model_file_read evidence; treat retrieval channel as coverage risk."
        )
    missing_source_card_write_source_ids = _missing_source_card_write_sources(source_map, triage_traces)
    passed = not (
        (requires_tool_call and tool_call_count == 0)
        or "Triage did not call finalize_source_map." in source_map.gaps
        or evidence_count == 0
        or invalid_source_ids
        or invalid_version_ids
        or invalid_evidence_ids
        or missing_source_card_evidence_source_ids
        or missing_source_card_write_source_ids
        or ungrounded_relationships
        or missing_function_evidence
    )
    summary = "Source triage gate passed." if passed else "Source triage gate failed."
    if requires_tool_call and tool_call_count == 0:
        summary += " No triage reading/search tool calls were recorded."
    if "Triage did not call finalize_source_map." in source_map.gaps:
        summary += " Source triage did not finalize the source map."
    if evidence_count == 0:
        summary += " No valid persisted evidence ids were referenced."
    if missing_source_card_evidence_source_ids:
        summary += " Every SourceCard must contain at least one selected evidence id."
    if missing_model_read_source_ids:
        summary += " Warning: some PDF/document source cards lack direct model_file_read evidence."
    if missing_source_card_write_source_ids:
        summary += " Source cards must come from accepted upsert_source_card write actions."
    return SourceTriageGate(
        passed=passed,
        summary=summary,
        tool_call_count=tool_call_count,
        checked_source_count=len(referenced_source_ids),
        checked_evidence_count=evidence_count,
        invalid_source_ids=invalid_source_ids,
        invalid_source_version_ids=invalid_version_ids,
        invalid_evidence_ids=invalid_evidence_ids,
        missing_source_card_evidence_source_ids=missing_source_card_evidence_source_ids,
        missing_model_read_source_ids=missing_model_read_source_ids,
        missing_source_card_write_source_ids=missing_source_card_write_source_ids,
        ungrounded_relationships=ungrounded_relationships,
        missing_function_evidence=missing_function_evidence,
        warnings=warnings,
    )


async def mark_cited_evidence(db: AsyncSession, *, generation_run_id: uuid.UUID, evidence_ids: list[str]) -> None:
    for evidence_id in _dedupe(evidence_ids):
        if not _uuid_or_none(evidence_id):
            continue
        item = await db.get(EvidenceItem, uuid.UUID(evidence_id))
        if item is None or item.generation_run_id != generation_run_id:
            continue
        item.metadata_ = {
            **(item.metadata_ or {}),
            "usage_status": EVIDENCE_STATUS_CITED,
            "cited": True,
        }
    await db.flush()


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
    metadata = {"usage_status": EVIDENCE_STATUS_OBSERVED, **(metadata or {})}
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
            "usage_status": (existing.metadata_ or {}).get("usage_status") or metadata.get("usage_status"),
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
        metadata_=metadata,
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
    if not version.storage_path or version.kind == SourceRepresentationKind.ORIGINAL_URL:
        return ""
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE and not _looks_text_readable_path(version.storage_path):
        return ""
    try:
        return download_text(get_minio_client(), version.storage_path)
    except Exception:
        return ""


def _looks_text_readable_path(path: str) -> bool:
    lowered = path.lower().split("?", 1)[0]
    return lowered.endswith((".txt", ".md", ".markdown", ".html", ".htm", ".json", ".csv", ".tsv", ".xml", ".srt", ".vtt"))


_MODEL_FILE_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "supports_query": {"type": "boolean"},
        "support_level": {"type": "string", "enum": ["direct", "supported", "weak", "unsupported", "not_found"]},
        "quote_text": {"type": "string"},
        "snippet_text": {"type": "string"},
        "support_summary": {"type": "string"},
        "uncertainty": {"type": "string"},
        "locator_text": {"type": "string"},
        "locator_type": {"type": "string", "enum": ["page", "section", "timestamp", "provider_snippet", "source_level", "unknown"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "supports_query",
        "support_level",
        "quote_text",
        "snippet_text",
        "support_summary",
        "uncertainty",
        "locator_text",
        "locator_type",
        "confidence",
    ],
}


def _model_file_read_prompt(
    *,
    focus_question: str,
    claim_text: str,
    purpose: str,
    locator_hint: str,
    source_id: str,
    source_version_id: str,
    source_kind: str,
) -> str:
    return (
        "Read the attached file as source evidence for Podgen Generation V2.\n"
        f"source_id: {source_id}\n"
        f"source_version_id: {source_version_id}\n"
        f"source_kind: {source_kind}\n"
        f"purpose: {purpose}\n"
        f"focus_question: {focus_question}\n"
        f"claim_text: {claim_text}\n"
        f"locator_hint: {locator_hint}\n\n"
        "Task:\n"
        "- Use only the attached file.\n"
        "- Find the strongest relevant support for the focus question or claim.\n"
        "- Extract a direct quote when one is available; otherwise provide a concise snippet-like paraphrase grounded in the file.\n"
        "- Provide support_summary that explains what the file supports and any uncertainty.\n"
        "- Include locator_text only if the file provides a useful page, section, heading, timestamp, or filename hint. Page numbers are helpful but optional.\n"
        "- If the file does not support the claim/question, set supports_query=false and support_level=unsupported or not_found.\n"
    )


def _parse_model_file_read_result(output_text: str) -> dict[str, Any]:
    try:
        data = json.loads(output_text or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Model file read returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Model file read returned a non-object JSON payload")
    normalized = {
        "supports_query": bool(data.get("supports_query")),
        "support_level": str(data.get("support_level") or "unsupported"),
        "quote_text": str(data.get("quote_text") or ""),
        "snippet_text": str(data.get("snippet_text") or ""),
        "support_summary": str(data.get("support_summary") or ""),
        "uncertainty": str(data.get("uncertainty") or ""),
        "locator_text": str(data.get("locator_text") or ""),
        "locator_type": str(data.get("locator_type") or EvidenceLocatorType.UNKNOWN),
        "confidence": _confidence(data.get("confidence")),
    }
    if normalized["support_level"] not in {"direct", "supported", "weak", "unsupported", "not_found"}:
        normalized["support_level"] = "unsupported"
    if normalized["locator_type"] not in {
        EvidenceLocatorType.PAGE,
        EvidenceLocatorType.SECTION,
        EvidenceLocatorType.TIMESTAMP,
        EvidenceLocatorType.PROVIDER_SNIPPET,
        EvidenceLocatorType.SOURCE_LEVEL,
        EvidenceLocatorType.UNKNOWN,
    }:
        normalized["locator_type"] = EvidenceLocatorType.UNKNOWN
    return normalized


def _model_file_read_snippet(read_result: dict[str, Any]) -> str:
    parts = []
    quote = str(read_result.get("quote_text") or "").strip()
    snippet = str(read_result.get("snippet_text") or "").strip()
    summary = str(read_result.get("support_summary") or "").strip()
    uncertainty = str(read_result.get("uncertainty") or "").strip()
    if quote:
        parts.append(f"Quote: {quote}")
    if snippet and snippet != quote:
        parts.append(f"Snippet: {snippet}")
    if summary:
        parts.append(f"Support summary: {summary}")
    if uncertainty:
        parts.append(f"Uncertainty: {uncertainty}")
    return "\n".join(parts)


def _model_read_evidence_strength(read_result: dict[str, Any]) -> str:
    support_level = str(read_result.get("support_level") or "")
    if support_level == "direct" and str(read_result.get("quote_text") or "").strip():
        return EvidenceStrength.EXACT
    if support_level in {"direct", "supported"}:
        return EvidenceStrength.SNIPPET
    if support_level == "weak":
        return EvidenceStrength.WEAK
    return EvidenceStrength.WEAK


def _model_read_locator_type(read_result: dict[str, Any]) -> str:
    locator_type = str(read_result.get("locator_type") or EvidenceLocatorType.UNKNOWN)
    if locator_type in {
        EvidenceLocatorType.PAGE,
        EvidenceLocatorType.SECTION,
        EvidenceLocatorType.TIMESTAMP,
        EvidenceLocatorType.PROVIDER_SNIPPET,
        EvidenceLocatorType.SOURCE_LEVEL,
        EvidenceLocatorType.UNKNOWN,
    }:
        return locator_type
    return EvidenceLocatorType.UNKNOWN


def _model_read_version_priority(kind: str) -> int:
    order = {
        SourceRepresentationKind.ORIGINAL_FILE: 0,
        SourceRepresentationKind.PAGE_MARKED_TEXT: 1,
        SourceRepresentationKind.EXTRACTED_TEXT: 2,
        SourceRepresentationKind.TRANSCRIPT: 2,
        SourceRepresentationKind.RENDERED_SNAPSHOT: 3,
        SourceRepresentationKind.FETCHED_HTML: 4,
    }
    return order.get(kind, 100)


def _model_read_upload_payload(version: SourceVersion) -> tuple[str, bytes]:
    from app.generation_v2.source_corpus import _version_upload_payload

    return _version_upload_payload(version)


def _unsupported_model_read_selected_as_support(item: EvidenceItem, *, purpose: str, role: str) -> bool:
    if item.retrieval_channel != EvidenceRetrievalChannel.MODEL_FILE_READ:
        return False
    metadata = item.metadata_ or {}
    support_level = str(metadata.get("support_level") or "")
    if bool(metadata.get("supports_query", True)) and support_level not in {"weak", "unsupported", "not_found"}:
        return False
    allowed = {
        "gap",
        "gaps",
        "caution",
        "uncertainty",
        "contradiction",
        "negative_finding",
        "no_support",
        "not_found",
    }
    purpose_role = {part.lower().strip() for part in [purpose, role] if part}
    return not bool(purpose_role.intersection(allowed))


def _quote_text_matches_evidence(quote: str, snippet: str) -> bool:
    normalized_quote = _normalize(quote)
    normalized_snippet = _normalize(snippet)
    if not normalized_quote or not normalized_snippet:
        return False
    if normalized_quote in normalized_snippet:
        return True
    quote_tokens = set(_query_tokens(normalized_quote))
    if not quote_tokens:
        return False
    overlap = quote_tokens.intersection(_query_tokens(normalized_snippet))
    return len(overlap) / max(1, len(quote_tokens)) >= 0.75


def _query_tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[^\W_]{3,}", (text or "").lower(), flags=re.UNICODE) if token]


def _source_map_evidence_ids(source_map: SourceMap) -> list[str]:
    ids = [*source_map.evidence_ids]
    ids.extend(eid for card in source_map.source_cards for eid in card.evidence_ids)
    ids.extend(quote.evidence_id for quote in source_map.quote_cards if quote.evidence_id)
    ids.extend(eid for item in source_map.source_triage.ranked_sources for eid in item.evidence_ids)
    ids.extend(eid for values in source_map.source_triage.top_evidence_by_function.values() for eid in values)
    ids.extend(eid for cluster in source_map.source_triage.source_clusters for eid in cluster.evidence_ids)
    ids.extend(eid for relation in source_map.source_triage.source_relationships for eid in relation.evidence_ids)
    return _dedupe([str(item) for item in ids if item])


def _trace_accepted(trace: V2ToolTrace) -> bool:
    output = getattr(trace, "output_json", None) or {}
    return bool(output.get("accepted", True))


def _selected_evidence_ids_from_actions(traces: list[V2ToolTrace], evidence_by_id: dict[str, EvidenceItem]) -> list[str]:
    selected = [
        evidence_id
        for evidence_id, evidence in evidence_by_id.items()
        if (evidence.metadata_ or {}).get("usage_status") in {EVIDENCE_STATUS_SELECTED, EVIDENCE_STATUS_CITED}
    ]
    for trace in traces:
        if trace.tool_name != "select_evidence":
            continue
        evidence_id = str((trace.input_json or {}).get("evidence_id") or "")
        if evidence_id:
            selected.append(evidence_id)
    return _dedupe(selected)


def _selected_role(evidence: EvidenceItem) -> str:
    metadata = evidence.metadata_ or {}
    return str(metadata.get("selected_role") or metadata.get("selected_purpose") or evidence.purpose or "background")


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


def _missing_source_card_evidence_sources(
    source_map: SourceMap,
    evidence_by_id: dict[str, EvidenceItem],
    selected_evidence_ids: set[str],
    *,
    invalid_evidence_ids: set[str],
) -> list[str]:
    missing = []
    for card in source_map.source_cards:
        valid_card_evidence = [
            evidence_id
            for evidence_id in card.evidence_ids
            if evidence_id in evidence_by_id
            and evidence_id in selected_evidence_ids
            and evidence_id not in invalid_evidence_ids
            and str(evidence_by_id[evidence_id].source_id) == card.source_id
        ]
        if not valid_card_evidence:
            missing.append(card.source_id)
    return sorted(_dedupe(missing))


def _missing_model_read_sources(
    source_map: SourceMap,
    evidence_by_id: dict[str, EvidenceItem],
    selected_evidence_ids: set[str],
) -> list[str]:
    missing: list[str] = []
    for card in source_map.source_cards:
        if not _source_card_needs_model_read(card):
            continue
        source_top_evidence_ids = {
            evidence_id
            for values in source_map.source_triage.top_evidence_by_function.values()
            for evidence_id in values
            if evidence_id in evidence_by_id and str(evidence_by_id[evidence_id].source_id) == card.source_id
        }
        model_read_candidate_ids = set(card.evidence_ids).union(source_top_evidence_ids)
        candidate_evidence = [
            evidence_by_id[evidence_id]
            for evidence_id in model_read_candidate_ids
            if evidence_id in evidence_by_id and evidence_id in selected_evidence_ids
        ]
        if not candidate_evidence:
            missing.append(card.source_id)
            continue
        has_model_read = any(
            evidence_id in evidence_by_id
            and evidence_id in selected_evidence_ids
            and getattr(evidence_by_id[evidence_id], "retrieval_channel", None) == EvidenceRetrievalChannel.MODEL_FILE_READ
            and str(evidence_by_id[evidence_id].source_id) == card.source_id
            and evidence_by_id[evidence_id].evidence_strength in {EvidenceStrength.EXACT, EvidenceStrength.SNIPPET}
            for evidence_id in model_read_candidate_ids
        )
        if not has_model_read:
            missing.append(card.source_id)
    return sorted(_dedupe(missing))


def _missing_source_card_write_sources(source_map: SourceMap, traces: list[V2ToolTrace]) -> list[str]:
    upserted = {
        str((getattr(trace, "input_json", None) or {}).get("source_id") or "")
        for trace in traces
        if getattr(trace, "tool_name", "") == "upsert_source_card" and _trace_accepted(trace)
    }
    return sorted(card.source_id for card in source_map.source_cards if card.source_id not in upserted)


def _source_card_needs_model_read(card: SourceCard) -> bool:
    source_type = str(card.source_type or "").lower()
    if source_type == "pdf":
        return True
    summary = f"{card.title} {card.url or ''} {card.representation_summary}".lower()
    return ".pdf" in summary or "original_file" in summary


def _relationship_kind(value: str) -> str:
    allowed = {"primary_to_secondary", "duplicate", "near_duplicate", "contradicts", "updates", "background_for"}
    return value if value in allowed else "background_for"


def _hosted_result_file_id(result: dict[str, Any]) -> str | None:
    for key in ("file_id", "fileId", "id"):
        value = result.get(key)
        if value:
            return str(value)
    return None


def _hosted_result_text(result: dict[str, Any]) -> str:
    text = result.get("text")
    if isinstance(text, str):
        return text
    content = result.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        return "\n\n".join(part for part in parts if part).strip()
    return str(result.get("snippet") or result.get("quote") or "")


def _safe_metadata(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    for key in ("file_id", "filename", "score", "attributes", "result_index"):
        if key in result:
            allowed[key] = result[key]
    return allowed


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


def _window_for_locator(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str]:
    normalized = text.strip()
    if not normalized:
        return "", locator or "source window", EvidenceLocatorType.UNKNOWN
    page_window = _page_window(normalized, locator=locator, max_chars=max_chars)
    if page_window:
        return page_window
    timestamp_window = _timestamp_window(normalized, locator=locator, max_chars=max_chars)
    if timestamp_window:
        return timestamp_window
    heading_window = _heading_window(normalized, locator=locator, max_chars=max_chars)
    if heading_window:
        return heading_window
    if locator:
        idx = normalized.lower().find(locator.lower())
        if idx >= 0:
            start = max(0, idx - max_chars // 4)
            end = min(len(normalized), start + max_chars)
            return normalized[start:end], f"around phrase: {locator}", EvidenceLocatorType.SECTION
    return normalized[:max_chars], locator or "source beginning", EvidenceLocatorType.SOURCE_LEVEL


def _source_outline(text: str, *, max_items: int) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    items: list[dict[str, Any]] = []
    for page in _page_markers(text)[:max_items]:
        items.append({"locator": f"page {page['page']}", "locator_type": EvidenceLocatorType.PAGE, "preview": page["line"][:240]})
    if len(items) < max_items:
        for timestamp in _timestamp_markers(text)[: max_items - len(items)]:
            items.append(
                {
                    "locator": timestamp["timestamp"],
                    "locator_type": EvidenceLocatorType.TIMESTAMP,
                    "preview": timestamp["line"][:240],
                }
            )
    if len(items) < max_items:
        for heading in _heading_markers(text)[: max_items - len(items)]:
            items.append({"locator": heading["heading"], "locator_type": EvidenceLocatorType.SECTION, "preview": heading["line"][:240]})
    return items


def _page_window(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    page_range = _parse_page_locator(locator)
    if not page_range:
        return None
    start_page, end_page = page_range
    markers = _page_markers(text)
    if not markers:
        return None
    start_marker = next((marker for marker in markers if marker["page"] == start_page), None)
    if start_marker is None:
        return None
    end_marker = next((marker for marker in markers if marker["page"] > end_page), None)
    start = int(start_marker["start"])
    end = int(end_marker["start"]) if end_marker else len(text)
    snippet = text[start:end].strip()[:max_chars]
    label = f"page {start_page}" if start_page == end_page else f"pages {start_page}-{end_page}"
    return snippet, label, EvidenceLocatorType.PAGE


def _timestamp_window(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    target_seconds = _parse_timestamp(locator)
    if target_seconds is None:
        return None
    markers = _timestamp_markers(text)
    if not markers:
        return None
    nearest = min(markers, key=lambda marker: abs(int(marker["seconds"]) - target_seconds))
    start = max(0, int(nearest["start"]) - max_chars // 3)
    end = min(len(text), start + max_chars)
    return text[start:end].strip(), str(nearest["timestamp"]), EvidenceLocatorType.TIMESTAMP


def _heading_window(text: str, *, locator: str, max_chars: int) -> tuple[str, str, str] | None:
    if not locator.strip():
        return None
    lower = locator.lower()
    for heading in _heading_markers(text):
        if lower in str(heading["heading"]).lower():
            start = int(heading["start"])
            end = min(len(text), start + max_chars)
            return text[start:end].strip(), str(heading["heading"]), EvidenceLocatorType.SECTION
    return None


def _search_text_windows(text: str, *, query: str, max_windows: int, max_chars: int) -> list[dict[str, Any]]:
    tokens = _query_tokens(query)
    if not tokens:
        snippet = text.strip()[:max_chars]
        return [{"snippet_text": snippet, "locator_text": "source beginning", "locator_type": EvidenceLocatorType.SOURCE_LEVEL, "score": 0.0}]
    matches: list[dict[str, Any]] = []
    lower = text.lower()
    seen_starts: set[int] = set()
    for token in tokens[:8]:
        for match in re.finditer(re.escape(token), lower):
            start = max(0, match.start() - max_chars // 4)
            start = max(marker for marker in [0, start] if marker >= 0)
            if any(abs(start - seen) < max_chars // 3 for seen in seen_starts):
                continue
            seen_starts.add(start)
            end = min(len(text), start + max_chars)
            snippet = text[start:end].strip()
            locator = _nearest_locator_for_position(text, match.start()) or f"around phrase: {token}"
            score = sum(snippet.lower().count(item) for item in tokens[:8])
            matches.append({"snippet_text": snippet, "locator_text": locator, "locator_type": _locator_type_for_label(locator), "score": float(score)})
            if len(matches) >= max_windows:
                return sorted(matches, key=lambda item: item["score"], reverse=True)
    return sorted(matches, key=lambda item: item["score"], reverse=True)[:max_windows]


def _page_markers(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?im)^\s*(?:-{2,}\s*)?(?:\[?\s*(?:page|p\.)\s+(\d+)\s*\]?)(?:\s*-{2,})?\s*$")
    return [
        {
            "page": int(match.group(1)),
            "start": match.start(),
            "line": match.group(0).strip(),
        }
        for match in pattern.finditer(text)
    ]


def _heading_markers(text: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        is_heading = bool(re.match(r"^#{1,6}\s+\S", stripped)) or (
            4 <= len(stripped) <= 120
            and not stripped.endswith(".")
            and stripped[:1].isupper()
            and len(stripped.split()) <= 14
        )
        if is_heading:
            markers.append({"heading": stripped.lstrip("# ").strip(), "start": offset, "line": stripped})
        offset += len(line)
    return markers


def _timestamp_markers(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?m)(?:^|\s|\[)(\d{1,2}:\d{2}(?::\d{2})?)(?:\]|\s|$)")
    markers = []
    for match in pattern.finditer(text):
        seconds = _parse_timestamp(match.group(1))
        if seconds is None:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        markers.append({"timestamp": match.group(1), "seconds": seconds, "start": match.start(), "line": text[line_start:line_end].strip()})
    return markers


def _parse_page_locator(locator: str) -> tuple[int, int] | None:
    match = re.search(r"\bpages?\s+(\d+)(?:\s*[-–]\s*(\d+))?\b|\bp\.\s*(\d+)\b", locator or "", flags=re.IGNORECASE)
    if not match:
        return None
    start = int(match.group(1) or match.group(3))
    end = int(match.group(2) or start)
    return min(start, end), max(start, end)


def _parse_timestamp(value: str) -> int | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", value or "")
    if not match:
        return None
    first, second, third = match.groups()
    if third is None:
        return int(first) * 60 + int(second)
    return int(first) * 3600 + int(second) * 60 + int(third)


def _nearest_locator_for_position(text: str, position: int) -> str | None:
    candidates: list[tuple[int, str]] = []
    for marker in _page_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), f"page {marker['page']}"))
    for marker in _timestamp_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["timestamp"])))
    for marker in _heading_markers(text):
        if int(marker["start"]) <= position:
            candidates.append((int(marker["start"]), str(marker["heading"])))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _locator_type_for_label(label: str) -> str:
    if _parse_page_locator(label):
        return EvidenceLocatorType.PAGE
    if _parse_timestamp(label) is not None:
        return EvidenceLocatorType.TIMESTAMP
    if label.startswith("around phrase:"):
        return EvidenceLocatorType.SECTION
    return EvidenceLocatorType.SECTION


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


def _version_can_back_local_window(version: SourceVersion) -> bool:
    if version.status != V2RecordStatus.READY or not version.storage_path:
        return False
    if version.kind == SourceRepresentationKind.ORIGINAL_FILE:
        return _looks_text_readable_path(version.storage_path)
    return version.kind in {
        SourceRepresentationKind.PAGE_MARKED_TEXT,
        SourceRepresentationKind.EXTRACTED_TEXT,
        SourceRepresentationKind.TRANSCRIPT,
        SourceRepresentationKind.RENDERED_SNAPSHOT,
        SourceRepresentationKind.FETCHED_HTML,
    }


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
