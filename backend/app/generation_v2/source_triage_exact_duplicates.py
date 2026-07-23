from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select

from app.generation_v2.tools.evidence_refs import SourceRefContext
from app.models.generation_v2 import SourceVersion, V2RecordStatus


async def exact_duplicate_predecessor(
    db: Any,
    *,
    ref_context: SourceRefContext,
    ordered_source_refs: list[str],
    source_ref: str,
) -> dict[str, Any] | None:
    """Return an earlier source sharing a byte-identical persisted representation."""

    ordered = [str(value) for value in ordered_source_refs if str(value)]
    if source_ref not in ordered:
        return None
    predecessors = ordered[: ordered.index(source_ref)]
    if not predecessors:
        return None

    relevant_refs = [*predecessors, source_ref]
    source_ids_by_ref = {
        ref: ref_context.source_id_by_ref.get(ref, "") for ref in relevant_refs
    }
    version_ids: list[uuid.UUID] = []
    for source_id in source_ids_by_ref.values():
        card = ref_context.card_by_source_id.get(source_id)
        for raw_version_id in getattr(card, "source_version_ids", []) or []:
            try:
                version_ids.append(uuid.UUID(str(raw_version_id)))
            except (TypeError, ValueError):
                continue
    if not version_ids:
        return None

    rows = (
        await db.execute(
            select(
                SourceVersion.source_id,
                SourceVersion.kind,
                SourceVersion.content_hash,
            ).where(
                SourceVersion.id.in_(version_ids),
                SourceVersion.status == V2RecordStatus.READY,
                SourceVersion.content_hash.is_not(None),
            )
        )
    ).all()
    fingerprints_by_source_id: dict[str, set[tuple[str, str]]] = {}
    for source_id, kind, content_hash in rows:
        fingerprint = (str(kind or ""), str(content_hash or ""))
        if not fingerprint[1]:
            continue
        fingerprints_by_source_id.setdefault(str(source_id), set()).add(fingerprint)

    target_fingerprints = fingerprints_by_source_id.get(
        source_ids_by_ref[source_ref],
        set(),
    )
    for predecessor_ref in predecessors:
        matches = sorted(
            target_fingerprints
            & fingerprints_by_source_id.get(source_ids_by_ref[predecessor_ref], set())
        )
        if matches:
            return {
                "source_ref": predecessor_ref,
                "matched_representation_kinds": sorted({kind for kind, _hash in matches}),
            }
    return None


__all__ = ["exact_duplicate_predecessor"]
