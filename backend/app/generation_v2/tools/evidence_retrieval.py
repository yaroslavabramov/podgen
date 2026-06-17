from __future__ import annotations

import io
import re
import uuid
from typing import Any

from sqlalchemy import or_, select

from app.config import settings
from app.generation_v2.artifacts import save_tool_trace
from app.generation_v2.debug_dump import dump_v2_debug
from app.observability import safe_set_run, trace_span
from app.models.chunk import Chunk
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceRetrievalChannel,
    EvidenceStrength,
    ProviderCorpusFile,
    SourceVersion,
    V2RecordStatus,
)
from app.services.llm import get_openai_client
from app.generation_v2.tools.evidence_store import evidence_payload, persist_evidence_item
from app.generation_v2.tools.evidence_types import _CHUNK_BACKED_VERSION_KINDS
from app.generation_v2.tools.evidence_utils import (
    _MODEL_FILE_READ_SCHEMA,
    _chunk_version_priority,
    _model_file_read_prompt,
    _model_read_upload_payload,
    _model_read_version_priority,
    _normalize,
    _parse_model_file_read_result,
    _provider_filters,
    _provider_snippet,
    _query_tokens,
    _readable_version_priority,
    _retrieval_channel_for_version,
    _select_chunk_window,
    _source_outline,
    _source_version_text,
    _uuid_list,
    _uuid_or_none,
    _version_can_back_local_window,
    _window_for_locator,
)


class EvidenceRetrievalMixin:
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
        instructions = (
            "You are a careful source reader. Use only the attached input_file. "
            "Return JSON only. Do not rely on memory or outside knowledge."
        )
        with trace_span(
            "generation_v2.corpus_evidence.model_file_read",
            run_type="llm",
            inputs={
                "prompt_chars": len(prompt),
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "provider_file_id": provider_file.provider_file_id,
                "purpose": purpose,
            },
            metadata={
                "provider": "openai",
                "model": settings.generation_v2_openai_model,
                "phase": "v2_corpus_evidence",
                "agent_name": "corpus_evidence_api",
            },
            tags=["generation-v2", "openai", "input-file", "model-read"],
        ) as run_tree:
            try:
                response = await client.responses.create(
                    model=settings.generation_v2_openai_model,
                    instructions=instructions,
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
            except Exception as exc:
                dump_v2_debug(
                    run_id=str(self.run.id),
                    phase="v2_corpus_evidence",
                    agent_name="corpus_evidence_api",
                    dump_type="model-file-read-exception",
                    payload={
                        "run_id": str(self.run.id),
                        "source_id": str(version.source_id),
                        "source_version_id": str(version.id),
                        "provider_file_id": provider_file.provider_file_id,
                        "model_read_file_id": input_file_id,
                        "instructions": instructions,
                        "prompt": prompt,
                        "schema": _MODEL_FILE_READ_SCHEMA,
                        "error": str(exc),
                    },
                )
                safe_set_run(run_tree, outputs={"status": "exception", "error": str(exc)})
                raise
        output_text = getattr(response, "output_text", "") or ""
        read_result = _parse_model_file_read_result(output_text)
        read_result["_model_read_file_id"] = input_file_id
        safe_set_run(
            run_tree,
            outputs={
                "response_id": getattr(response, "id", None),
                "output_text_chars": len(output_text),
                "supports_query": read_result.get("supports_query"),
                "support_level": read_result.get("support_level"),
            },
        )
        dump_v2_debug(
            run_id=str(self.run.id),
            phase="v2_corpus_evidence",
            agent_name="corpus_evidence_api",
            dump_type="model-file-read-parsed",
            payload={
                "run_id": str(self.run.id),
                "source_id": str(version.source_id),
                "source_version_id": str(version.id),
                "provider_file_id": provider_file.provider_file_id,
                "model_read_file_id": input_file_id,
                "instructions": instructions,
                "prompt": prompt,
                "schema": _MODEL_FILE_READ_SCHEMA,
                "raw_output": output_text,
                "parsed": read_result,
                "response_id": getattr(response, "id", None),
            },
        )
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


__all__ = ["EvidenceRetrievalMixin"]
