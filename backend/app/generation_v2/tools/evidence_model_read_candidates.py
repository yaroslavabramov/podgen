from __future__ import annotations

import uuid

from sqlalchemy import select

from app.generation_v2.tools.evidence_utils import _model_read_version_priority, _uuid_list, _uuid_or_none
from app.models.generation_v2 import ProviderCorpusFile, SourceVersion, V2RecordStatus


async def resolve_provider_file_candidates_for_model_read(
    toolset,
    *,
    source_id: str,
    source_version_id: str,
) -> list[tuple[ProviderCorpusFile, SourceVersion]]:
    if toolset.provider_index is None:
        return []
    selected_source_ids = {str(item) for item in toolset.run_view.selected_source_ids}
    selected_version_ids = {str(item) for item in toolset.run_view.selected_source_version_ids}
    target_source_id = ""
    if source_version_id and _uuid_or_none(source_version_id):
        if source_version_id not in selected_version_ids:
            return []
        version = await toolset.db.get(SourceVersion, uuid.UUID(source_version_id))
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
        ProviderCorpusFile.provider_index_id == toolset.provider_index.id,
        ProviderCorpusFile.status == V2RecordStatus.READY,
        ProviderCorpusFile.provider_file_id.is_not(None),
        ProviderCorpusFile.source_version_id.in_(_uuid_list(list(selected_version_ids))),
        ProviderCorpusFile.source_id == uuid.UUID(target_source_id),
    ]
    result = await toolset.db.execute(select(ProviderCorpusFile).where(*filters))
    candidates: list[tuple[ProviderCorpusFile, SourceVersion]] = []
    for provider_file in result.scalars():
        version = await toolset.db.get(SourceVersion, provider_file.source_version_id)
        if not version or not toolset._in_run_corpus_view(provider_file.source_id, provider_file.source_version_id):
            continue
        candidates.append((provider_file, version))
    candidates.sort(key=lambda item: _model_read_version_priority(item[1].kind))
    return candidates


__all__ = ["resolve_provider_file_candidates_for_model_read"]
