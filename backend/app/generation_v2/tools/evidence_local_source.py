from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.source_content_readiness import source_version_model_eligible
from app.generation_v2.tools.evidence_local_search import (
    local_fallback_search as _local_fallback_search_impl,
    source_version_fallback as _source_version_fallback_impl,
)
from app.generation_v2.tools.evidence_local_windows import (
    read_source_text_page as _read_source_text_page_impl,
    read_source_window as _local_read_source_window,
    resolve_local_fallback_source_version as _resolve_local_fallback_source_version,
    search_source_text_ranges as _search_source_text_ranges_impl,
    sequential_source_window_page as _sequential_source_window_page_impl,
    source_allows_timestamp_locators as _source_allows_timestamp_locators_impl,
    source_windows as _local_source_windows,
)
from app.generation_v2.tools.evidence_source_text import best_readable_source_version
from app.generation_v2.tools.evidence_source_text import (
    SourceTextLoadResult,
    load_source_version_text,
)
from app.generation_v2.tools.evidence_utils import (
    _uuid_list,
    _uuid_or_none,
)
from app.models.generation_v2 import (
    EvidenceItem,
    SourceVersion,
    V2RecordStatus,
)


def _source_version_text(version: SourceVersion) -> SourceTextLoadResult:
    return load_source_version_text(version)


class EvidenceLocalSourceMixin:
    def _cached_source_version_text(self, version: SourceVersion) -> str:
        """Download/extract one immutable representation once per toolset lifetime."""

        cache = getattr(self, "_source_version_text_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_source_version_text_cache", cache)
        key = (str(version.id), str(version.content_hash or ""))
        if key in cache:
            return str(cache[key])
        loaded = _source_version_text(version)
        if isinstance(loaded, SourceTextLoadResult):
            text = loaded.text
            status = loaded.status
            error_detail = loaded.error_detail
        else:
            # Tests and narrow adapters may replace the loader with a plain
            # validated string.
            text = str(loaded)
            status = "ready" if text else "empty"
            error_detail = ""
        errors = getattr(self, "_source_version_text_errors", None)
        if not isinstance(errors, dict):
            errors = {}
            setattr(self, "_source_version_text_errors", errors)
        if status != "ready":
            errors[key] = {"status": status, "detail": error_detail}
            return ""
        errors.pop(key, None)
        # The set-agent normally owns only a small queue. Still bound the cache
        # so a long-lived API toolset cannot retain an entire large corpus.
        if len(cache) >= 8:
            cache.pop(next(iter(cache)))
        cache[key] = text
        return text

    def _source_version_text_error_payload(self, version: SourceVersion) -> dict[str, str] | None:
        key = (str(version.id), str(version.content_hash or ""))
        errors = getattr(self, "_source_version_text_errors", None)
        error = errors.get(key) if isinstance(errors, dict) else None
        if not isinstance(error, dict):
            return None
        status = str(error.get("status") or "")
        return {
            "error": {
                "storage_unavailable": "source_text_storage_temporarily_unavailable",
                "integrity_error": "source_text_integrity_error",
                "empty": "source_text_is_empty",
                "not_text_readable": "source_has_no_text_readable_representation",
            }.get(status, "source_text_unavailable"),
            "retryable": status == "storage_unavailable",
        }

    async def _local_fallback_search(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: list[str],
        limit: int,
    ) -> list[EvidenceItem]:
        return await _local_fallback_search_impl(
            self,
            query=query,
            purpose=purpose,
            source_ids=source_ids,
            limit=limit,
        )

    async def _source_version_fallback(
        self,
        *,
        query: str,
        purpose: str,
        source_ids: set[str],
        limit: int,
    ) -> list[EvidenceItem]:
        return await _source_version_fallback_impl(
            self,
            query=query,
            purpose=purpose,
            source_ids=source_ids,
            limit=limit,
        )

    def _in_run_corpus_view(self, source_id: uuid.UUID, source_version_id: uuid.UUID) -> bool:
        return str(source_id) in {str(item) for item in self.run_view.selected_source_ids} and str(
            source_version_id
        ) in {str(item) for item in self.run_view.selected_source_version_ids}

    async def _resolve_source_version(
        self,
        *,
        source_id: str,
        source_version_id: str,
        source_ref: str = "",
        version_ref: str = "",
    ) -> SourceVersion | None:
        selected_version_ids = set(str(item) for item in self.run_view.selected_source_version_ids)
        selected_source_ids = set(str(item) for item in self.run_view.selected_source_ids)
        if version_ref and not source_version_id:
            source_version_id = await self._source_version_id_from_ref(version_ref) or ""
        if source_ref and not source_id:
            source_id = await self._source_id_from_ref_or_id(source_ref) or ""
        scope_error = await self._triage_scope_error_for_source_ids([source_id] if source_id else [])
        if scope_error:
            return None
        if source_version_id and _uuid_or_none(source_version_id):
            version = await self.db.get(SourceVersion, uuid.UUID(source_version_id))
            if version is not None:
                scope_error = await self._triage_scope_error_for_source_ids([str(version.source_id)])
                if scope_error:
                    return None
            if (
                version
                and str(version.id) in selected_version_ids
                and str(version.source_id) in selected_source_ids
                and source_version_model_eligible(version)
            ):
                return version
            return None
        if not source_id or not _uuid_or_none(source_id) or source_id not in selected_source_ids:
            return None
        result = await self.db.execute(
            select(SourceVersion).where(
                SourceVersion.source_id == uuid.UUID(source_id),
                SourceVersion.id.in_(_uuid_list(list(selected_version_ids))),
                SourceVersion.status == V2RecordStatus.READY,
            )
        )
        versions = [version for version in result.scalars() if source_version_model_eligible(version)]
        return best_readable_source_version(versions, text_getter=self._cached_source_version_text)

    async def _resolve_local_fallback_source_version(
        self, *, source_id: str, source_version_id: str
    ) -> SourceVersion | None:
        return await _resolve_local_fallback_source_version(
            self, source_id=source_id, source_version_id=source_version_id
        )

    async def _read_source_window(
        self,
        version: SourceVersion,
        *,
        locator: str,
        max_chars: int,
    ) -> tuple[str, str, str, str]:
        return await _local_read_source_window(self, version, locator=locator, max_chars=max_chars)

    async def _read_source_text_page(
        self,
        version: SourceVersion,
        *,
        cursor: str,
        max_chars: int,
    ) -> dict[str, Any]:
        return await _read_source_text_page_impl(
            self,
            version,
            cursor=cursor,
            max_chars=max_chars,
        )

    async def _search_source_text_ranges(
        self,
        version: SourceVersion,
        *,
        query: str,
        max_results: int,
        max_chars: int,
    ) -> list[dict[str, Any]]:
        return await _search_source_text_ranges_impl(
            self,
            version,
            query=query,
            max_results=max_results,
            max_chars=max_chars,
        )

    async def _source_windows(self, version: SourceVersion, *, max_windows: int) -> list[dict[str, Any]]:
        return await _local_source_windows(self, version, max_windows=max_windows)

    async def _sequential_source_window_page(
        self,
        version: SourceVersion,
        *,
        cursor: str,
        max_windows: int,
        max_chars: int = 1600,
    ) -> dict[str, Any]:
        return await _sequential_source_window_page_impl(
            self,
            version,
            cursor=cursor,
            max_windows=max_windows,
            max_chars=max_chars,
        )

    async def _source_allows_timestamp_locators(self, version: SourceVersion) -> bool:
        return await _source_allows_timestamp_locators_impl(self, version)


__all__ = ["EvidenceLocalSourceMixin"]
