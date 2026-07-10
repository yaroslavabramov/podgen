from __future__ import annotations

from app.config import settings
from app.generation_v2.schemas import (
    SourceAcquisitionRound,
    SourceAcquisitionReport,
)
from app.generation_v2.source_acquisition_gaps import build_corpus_gap_report as build_corpus_gap_report
from app.generation_v2.source_acquisition_toolset import (
    SourceAcquisitionToolset,
)
from app.generation_v2.source_acquisition_validation import (
    normalize_candidate_url,
    validate_candidate_static,
)
from app.models.generation_run import GenerationRun


__all__ = [
    "SourceAcquisitionToolset",
    "acquisition_enabled",
    "build_corpus_gap_report",
    "normalize_candidate_url",
    "remaining_acquisition_rounds",
    "source_acquisition_report_from_rounds",
    "validate_candidate_static",
]


def acquisition_enabled(run: GenerationRun) -> bool:
    config = run.config or {}
    mode = str(config.get("source_acquisition_mode") or settings.generation_v2_source_acquisition_mode or "auto")
    return bool(settings.generation_v2_enable_source_acquisition and mode != "off")


def remaining_acquisition_rounds(used_rounds: int) -> int:
    return max(0, int(settings.generation_v2_source_acquisition_max_rounds) - used_rounds)


def source_acquisition_report_from_rounds(
    *,
    enabled: bool,
    exhausted: bool,
    rounds: list[SourceAcquisitionRound],
) -> SourceAcquisitionReport:
    return SourceAcquisitionReport(
        enabled=enabled,
        mode=settings.generation_v2_source_acquisition_mode,
        exhausted=exhausted,
        total_rounds=len(rounds),
        total_accepted=sum(item.accepted_count for item in rounds),
        total_rejected=sum(item.rejected_count for item in rounds),
        total_failed=sum(item.failed_count for item in rounds),
        total_duplicates=sum(item.duplicate_count for item in rounds),
        ingested_source_ids=sorted({source_id for item in rounds for source_id in item.ingested_source_ids}),
        unresolved_gap_ids=rounds[-1].unresolved_gap_ids if rounds else [],
        summary=rounds[-1].summary if rounds else "Source acquisition did not run.",
    )
