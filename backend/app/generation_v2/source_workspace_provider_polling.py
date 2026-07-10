from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.generation_v2 import SourceRepresentation, V2RecordStatus


async def commit_if_supported(db: AsyncSession) -> None:
    commit = getattr(db, "commit", None)
    if commit is not None:
        await commit()


async def poll_workspace_provider_file(
    db: AsyncSession,
    client: Any,
    provider_rep: SourceRepresentation,
    vector_store_id: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + max(30, int(settings.generation_v2_provider_upload_timeout_seconds))
    while True:
        vector_file = await workspace_retrieve_vector_store_file(client, vector_store_id, provider_rep.provider_file_id)
        if vector_file is None:
            provider_rep.status = V2RecordStatus.PENDING
            provider_rep.metadata_ = {
                **(provider_rep.metadata_ or {}),
                "upload_phase": "indexing",
                "last_provider_status": "retrieve_unavailable",
            }
            await db.flush()
            await commit_if_supported(db)
            return
        status = str(getattr(vector_file, "status", "") or "")
        provider_rep.metadata_ = {
            **(provider_rep.metadata_ or {}),
            "provider_vector_store_file_id": getattr(vector_file, "id", None),
            "last_provider_status": status,
        }
        if status == "completed":
            provider_rep.status = V2RecordStatus.READY
            provider_rep.error_message = None
            provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "ready"}
            await db.flush()
            await commit_if_supported(db)
            return
        if status in {"failed", "cancelled"}:
            provider_rep.status = V2RecordStatus.FAILED
            provider_rep.error_message = f"OpenAI vector store file status: {status}"
            provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "failed"}
            await db.flush()
            await commit_if_supported(db)
            return
        if asyncio.get_running_loop().time() >= deadline:
            provider_rep.status = V2RecordStatus.PENDING
            provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "indexing"}
            await db.flush()
            await commit_if_supported(db)
            return
        provider_rep.status = V2RecordStatus.PENDING
        provider_rep.metadata_ = {**(provider_rep.metadata_ or {}), "upload_phase": "indexing"}
        await db.flush()
        await commit_if_supported(db)
        await asyncio.sleep(1)


async def workspace_retrieve_vector_store_file(client: Any, vector_store_id: str, provider_file_id: str | None) -> Any | None:
    if not provider_file_id:
        return None
    try:
        return await asyncio.wait_for(
            client.vector_stores.files.retrieve(file_id=provider_file_id, vector_store_id=vector_store_id),
            timeout=workspace_provider_operation_timeout_seconds(),
        )
    except Exception:
        return None


def workspace_provider_operation_timeout_seconds() -> int:
    return max(10, min(60, int(settings.generation_v2_provider_upload_timeout_seconds)))


__all__ = [
    "poll_workspace_provider_file",
    "commit_if_supported",
    "workspace_provider_operation_timeout_seconds",
    "workspace_retrieve_vector_store_file",
]
