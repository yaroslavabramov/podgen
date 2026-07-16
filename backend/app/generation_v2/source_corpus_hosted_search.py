from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.source_content_readiness import source_version_model_eligible
from app.generation_v2.source_corpus_contracts import ProviderUploadPhase
from app.generation_v2.source_corpus_provider_attribute_sync import sync_provider_file_attributes
from app.generation_v2.source_corpus_provider_attributes import provider_attributes, provider_db_attributes
from app.models.generation_v2 import ProviderCorpusFile, ProviderCorpusIndex, RunCorpusView, SourceVersion, V2RecordStatus


async def prepare_run_hosted_file_search(
    db: AsyncSession,
    provider_index: ProviderCorpusIndex | None,
    run_view: RunCorpusView,
    *,
    sync_attributes_fn: Any = sync_provider_file_attributes,
) -> list[str]:
    """Backfill legacy provider attributes and return the exact safe hosted scope.

    Run preparation remains read-only. This compatibility repair happens only
    immediately before the sole hosted file-search call. A file is exposed only
    after its current run-pinned version is content-eligible and the provider
    accepted explicit fail-closed attributes. Hosted search is all-or-nothing:
    if any pinned file cannot be repaired, the agent keeps only the run-scoped
    local evidence tools instead of searching a silently incomplete corpus.
    """
    if (
        provider_index is None
        or provider_index.status != V2RecordStatus.READY
        or not provider_index.provider_vector_store_id
        or not settings.openai_api_key
        or str(getattr(run_view, "provider_index_id", "") or "") != str(provider_index.id)
    ):
        return []
    eligible_file_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in (getattr(run_view, "eligible_provider_file_ids", None) or [])
            if str(value).strip()
        )
    )
    selected_version_ids = {
        str(value).strip()
        for value in (getattr(run_view, "selected_source_version_ids", None) or [])
        if str(value).strip()
    }
    if not eligible_file_ids or not selected_version_ids:
        return []

    file_result = await db.execute(
        select(ProviderCorpusFile).where(
            ProviderCorpusFile.provider_index_id == provider_index.id,
            ProviderCorpusFile.provider_file_id.in_(eligible_file_ids),
            ProviderCorpusFile.status == V2RecordStatus.READY,
        )
    )
    provider_file_rows = list(file_result.scalars())
    # Missing or duplicated rows would make the hosted surface differ from the
    # immutable run view, so do not expose a partial scope.
    if len(provider_file_rows) != len(eligible_file_ids):
        return []
    provider_files_by_id = {str(row.provider_file_id): row for row in provider_file_rows}
    if set(provider_files_by_id) != set(eligible_file_ids):
        return []
    provider_files = [provider_files_by_id[file_id] for file_id in eligible_file_ids]
    if any(_provider_row_is_tombstoned(row) for row in provider_files):
        return []

    row_version_ids = [row.source_version_id for row in provider_files]
    if len(set(row_version_ids)) != len(row_version_ids) or any(
        str(version_id) not in selected_version_ids for version_id in row_version_ids
    ):
        return []
    version_result = await db.execute(select(SourceVersion).where(SourceVersion.id.in_(sorted(row_version_ids))))
    versions_by_id = {version.id: version for version in version_result.scalars()}

    candidates: list[tuple[ProviderCorpusFile, SourceVersion, dict[str, str | bool | float]]] = []
    for provider_file in provider_files:
        version = versions_by_id.get(provider_file.source_version_id)
        if (
            version is None
            or str(version.id) not in selected_version_ids
            or version.source_id != provider_file.source_id
            or version.project_id != provider_index.project_id
            or not source_version_model_eligible(version)
        ):
            return []
        desired = {
            **provider_attributes(version),
            # A run view may intentionally pin the last provider-ready version
            # while its replacement is still indexing. Exact version filters
            # make this compatibility reactivation safe for that run.
            "is_active": True,
            "agent_facing": True,
            "canonical_retrieval_surface": True,
            "content_confirmed": True,
        }
        candidates.append((provider_file, version, desired))

    for provider_file, _version, desired in candidates:
        # Reconciliation may already have written the desired attributes to
        # the DB row while adopting an older remote file. Force the remote
        # update so a local equality short-circuit cannot create a false-ready
        # hosted surface.
        synced = await sync_attributes_fn(provider_index, provider_file, desired, force=True)
        if not synced:
            return []

    ready_version_ids: list[str] = []
    for provider_file, version, desired in candidates:
        provider_file.attributes = provider_db_attributes(
            provider_file.attributes,
            ProviderUploadPhase.READY,
            desired,
        )
        ready_version_ids.append(str(version.id))
    if candidates:
        await db.flush()
    return ready_version_ids


def _provider_row_is_tombstoned(provider_file: ProviderCorpusFile) -> bool:
    if provider_file.cleanup_status:
        return True
    attributes = provider_file.attributes or {}
    if attributes.get("active_index_status") or attributes.get("excluded_reason"):
        return True
    for key in ("is_active", "agent_facing", "canonical_retrieval_surface"):
        if key in attributes and attributes[key] is not True:
            return True
    if "content_confirmed" in attributes and attributes["content_confirmed"] is not True:
        return True
    return "content_state" in attributes and attributes["content_state"] != "confirmed"


__all__ = ["prepare_run_hosted_file_search"]
