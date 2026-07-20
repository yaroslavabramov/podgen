from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.source_triage_workspace_persistence import (
    load_source_triage_workspace,
    save_source_triage_workspace,
)


FINALIZER_EVIDENCE_READS_FIELD = "source_triage_finalizer_evidence_reads"


async def record_finalizer_evidence_read(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    evidence_id: str,
    exact_read_complete: bool,
) -> None:
    if not exact_read_complete:
        return
    workspace = await load_source_triage_workspace(db, generation_run_id)
    if not workspace:
        return
    ledger = {
        str(value)
        for value in workspace.get(FINALIZER_EVIDENCE_READS_FIELD) or []
        if str(value)
    }
    ledger.add(str(evidence_id))
    workspace[FINALIZER_EVIDENCE_READS_FIELD] = sorted(ledger)
    await save_source_triage_workspace(
        db,
        generation_run_id=generation_run_id,
        workspace=workspace,
    )


def finalizer_has_full_evidence(
    workspace: dict[str, Any],
    *,
    evidence_id: str,
) -> bool:
    return str(evidence_id) in {
        str(value)
        for value in workspace.get(FINALIZER_EVIDENCE_READS_FIELD) or []
        if str(value)
    }


__all__ = [
    "FINALIZER_EVIDENCE_READS_FIELD",
    "finalizer_has_full_evidence",
    "record_finalizer_evidence_read",
]
