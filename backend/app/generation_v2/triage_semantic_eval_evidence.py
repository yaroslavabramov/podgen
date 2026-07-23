from __future__ import annotations

from copy import deepcopy
import uuid
from typing import Any

from sqlalchemy import select

from app.generation_v2.tools.evidence_exact_read import (
    EXACT_READ_PROGRESS_KEY,
    EXACT_READ_PROVENANCE_KEY,
)
from app.generation_v2.tools.evidence_store import persist_evidence_item
from app.generation_v2.tools.evidence_types import EVIDENCE_STATUS_SELECTED
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import EvidenceItem, EvidenceRetrievalChannel


async def clone_semantic_evidence(
    *,
    db: Any,
    source_run_id: uuid.UUID,
    target_run: GenerationRun,
    target_evidence_toolset: Any,
    evidence_ids: list[str],
) -> dict[str, str]:
    parsed_ids = parsed_evidence_ids(evidence_ids)
    result = await db.execute(select(EvidenceItem).where(EvidenceItem.id.in_(parsed_ids.values())))
    source_items = {str(item.id): item for item in result.scalars()}
    missing = sorted(set(parsed_ids) - set(source_items))
    wrong_run = sorted(
        evidence_id
        for evidence_id, item in source_items.items()
        if item.generation_run_id != source_run_id
    )
    if missing or wrong_run:
        raise RuntimeError(
            "Cannot clone frozen semantic evidence: "
            f"missing={missing}, wrong_run={wrong_run}"
        )
    mapping: dict[str, str] = {}
    for evidence_id in evidence_ids:
        item = source_items[evidence_id]
        metadata = deepcopy(item.metadata_ or {})
        metadata.pop(EXACT_READ_PROVENANCE_KEY, None)
        metadata.pop(EXACT_READ_PROGRESS_KEY, None)
        metadata.pop("cited", None)
        metadata.update(
            {
                "usage_status": EVIDENCE_STATUS_SELECTED,
                "semantic_eval_lineage": {
                    "source_run_id": str(source_run_id),
                    "source_evidence_id": evidence_id,
                    "source_version_id": str(item.source_version_id),
                    "retrieval_performed": False,
                },
            }
        )
        carried = await persist_evidence_item(
            db,
            generation_run_id=target_run.id,
            project_id=target_run.project_id,
            source_id=item.source_id,
            source_version_id=item.source_version_id,
            provider=item.provider,
            provider_vector_store_id=item.provider_vector_store_id,
            provider_file_id=item.provider_file_id,
            provider_vector_store_file_id=item.provider_vector_store_file_id,
            retrieval_channel=EvidenceRetrievalChannel.ATTEMPT_LINEAGE,
            purpose="triage_semantic_eval_lineage",
            score=item.score,
            snippet_text=item.snippet_text,
            normalized_quote_text=item.normalized_quote_text,
            locator_text=item.locator_text,
            locator_type=item.locator_type,
            evidence_strength=item.evidence_strength,
            metadata=metadata,
        )
        await target_evidence_toolset._mark_evidence_exact_read(
            carried,
            method="semantic_eval_lineage",
        )
        mapping[evidence_id] = str(carried.id)
    await db.flush()
    return mapping


def parsed_evidence_ids(evidence_ids: list[str]) -> dict[str, uuid.UUID]:
    parsed: dict[str, uuid.UUID] = {}
    invalid: list[str] = []
    for value in evidence_ids:
        try:
            parsed[str(value)] = uuid.UUID(str(value))
        except (TypeError, ValueError):
            invalid.append(str(value))
    if invalid:
        raise ValueError("semantic eval contains invalid evidence IDs: " + ", ".join(invalid))
    return parsed


__all__ = ["clone_semantic_evidence", "parsed_evidence_ids"]
