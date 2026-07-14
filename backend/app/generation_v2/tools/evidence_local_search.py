from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_utils import (
    _normalize,
    _readable_version_priority,
    _retrieval_channel_for_version,
    _search_text_windows,
    _uuid_list,
    _uuid_or_none,
)
from app.models.generation_v2 import (
    EvidenceItem,
    EvidenceLocatorType,
    EvidenceStrength,
    SourceVersion,
    V2RecordStatus,
)


def _source_version_text(version: SourceVersion) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


async def local_fallback_search(
    owner: Any,
    *,
    query: str,
    purpose: str,
    source_ids: list[str],
    limit: int,
) -> list[EvidenceItem]:
    if limit <= 0:
        return []
    allowed_sources = set(source_ids or [str(item) for item in owner.run_view.selected_source_ids])
    return await owner._source_version_fallback(
        query=query,
        purpose=purpose,
        source_ids=allowed_sources,
        limit=limit,
    )


async def source_version_fallback(
    owner: Any,
    *,
    query: str,
    purpose: str,
    source_ids: set[str],
    limit: int,
) -> list[EvidenceItem]:
    selected_version_ids = _uuid_list(owner.run_view.selected_source_version_ids)
    allowed_source_ids = [source_id for source_id in source_ids if _uuid_or_none(source_id)]
    if not selected_version_ids or not allowed_source_ids:
        return []
    result = await owner.db.execute(
        select(SourceVersion)
        .where(
            SourceVersion.status == V2RecordStatus.READY,
            SourceVersion.source_id.in_(_uuid_list(allowed_source_ids)),
            SourceVersion.id.in_(selected_version_ids),
        )
    )
    candidates: list[tuple[float, int, str, SourceVersion, dict[str, Any]]] = []
    for version in result.scalars():
        text = _source_version_text(version)
        if not text.strip():
            continue
        for window in _search_text_windows(
            text,
            query=query,
            max_windows=max(1, limit),
            max_chars=1800,
        ):
            candidates.append(
                (
                    float(window.get("score") or 0.0),
                    _readable_version_priority(version.kind),
                    str(version.id),
                    version,
                    window,
                )
            )
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    evidence: list[EvidenceItem] = []
    exact_locator_types = {
        EvidenceLocatorType.PAGE,
        EvidenceLocatorType.TIMESTAMP,
        EvidenceLocatorType.SECTION,
    }
    for idx, (score, _priority, _version_id, version, window) in enumerate(candidates[:limit]):
        snippet = str(window.get("snippet_text") or "")
        locator_type = str(window.get("locator_type") or EvidenceLocatorType.UNKNOWN)
        evidence.append(
            await persist_evidence_item(
                owner.db,
                generation_run_id=owner.run.id,
                project_id=owner.run.project_id,
                source_id=version.source_id,
                source_version_id=version.id,
                provider=None,
                retrieval_channel=_retrieval_channel_for_version(version),
                query_text=query,
                purpose=purpose,
                search_result_index=idx,
                score=score,
                snippet_text=snippet,
                normalized_quote_text=_normalize(snippet),
                locator_text=str(window.get("locator_text") or "source window"),
                locator_type=locator_type,
                evidence_strength=(
                    EvidenceStrength.EXACT
                    if locator_type in exact_locator_types
                    else EvidenceStrength.SNIPPET
                ),
                metadata={"immutable_source_version_search": True},
            )
        )
    return evidence


__all__ = ["local_fallback_search", "source_version_fallback"]
