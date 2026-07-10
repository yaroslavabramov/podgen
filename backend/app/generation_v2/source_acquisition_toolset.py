from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import CorpusGapReport, SourceAcquisitionCandidate, SourceAcquisitionRound
from app.generation_v2.source_acquisition_ingest import (
    native_first_ingest_candidate as _native_first_ingest_candidate,
    source_versions as _source_versions,
)
from app.generation_v2.source_acquisition_limits import (
    domain_cap_reached,
    existing_source_for_url,
    validation_rejection,
)
from app.generation_v2.source_acquisition_tool_specs import source_acquisition_agent_tools
from app.generation_v2.source_acquisition_validation import (
    normalize_candidate_url,
    short_hash as _short_hash,
    title_from_url as _title_from_url,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType
from app.models.source import IngestionStatus, Source, SourceType


@dataclass
class SourceAcquisitionToolset:
    db: AsyncSession
    run: GenerationRun
    gap_report: CorpusGapReport
    round_number: int
    ingested_count: int = 0
    candidates: list[SourceAcquisitionCandidate] = field(default_factory=list)
    finalized_summary: str = ""

    def agent_tools(self) -> list[AgentTool]:
        return source_acquisition_agent_tools(self)

    async def ingest_source_candidate_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        candidate = SourceAcquisitionCandidate(
            url=str(args.get("url") or ""),
            gap_ids=[str(item) for item in args.get("gap_ids") or [] if item],
            expected_editorial_function=str(args.get("expected_editorial_function") or ""),
            rationale=str(args.get("rationale") or ""),
            confidence=float(args.get("confidence") or 0.0),
            source_type_hint=str(args.get("source_type_hint") or "web"),
            authority_hint=str(args.get("authority_hint") or "unknown"),
        )
        candidate.normalized_url = normalize_candidate_url(candidate.url)
        rejection = await self._validation_rejection(candidate)
        if rejection:
            candidate.status = rejection[0]  # type: ignore[assignment]
            candidate.rejection_reason = rejection[1]
            candidate.ingest_status = "rejected"
            self.candidates.append(candidate)
            await self._save_candidate(candidate)
            return _candidate_tool_payload(candidate, accepted=False)

        self.ingested_count += 1
        source = Source(
            project_id=self.run.project_id,
            source_type=SourceType.WEB,
            original_url=candidate.normalized_url,
            title=_title_from_url(candidate.normalized_url),
            ingestion_status=IngestionStatus.PENDING,
            is_temporary=False,
            generation_run_id=self.run.id,
            metadata_={
                "auto_acquired_by_v2": True,
                "generation_run_id": str(self.run.id),
                "acquisition_round": self.round_number,
                "gap_ids": candidate.gap_ids,
                "candidate_url": candidate.url,
                "candidate_rationale": candidate.rationale,
                "expected_editorial_function": candidate.expected_editorial_function,
                "candidate_confidence": candidate.confidence,
                "source_type_hint": candidate.source_type_hint,
                "authority_hint": candidate.authority_hint,
                "vetting_status": "accepted",
            },
        )
        self.db.add(source)
        await self.db.commit()

        try:
            await asyncio.wait_for(
                _native_first_ingest_candidate(self.db, source.id),
                timeout=max(30, int(settings.generation_v2_source_acquisition_source_timeout_seconds)),
            )
            refreshed = await self.db.get(Source, source.id)
            if refreshed is None:
                raise RuntimeError("Source disappeared during acquisition ingest")
            source = refreshed
            candidate.status = "ingested"
            candidate.ingest_status = str(source.ingestion_status)
            candidate.fully_ingested = bool(source.fully_ingested)
            candidate.source_id = str(source.id)
            versions = await _source_versions(self.db, source.id)
            candidate.source_version_ids = [str(version.id) for version in versions]
            native = (source.metadata_ or {}).get("v2_native_ingest") or {}
            candidate.provider_file_count = int(native.get("provider_file_count") or 0)
            candidate.ready_provider_file_count = int(native.get("ready_provider_file_count") or 0)
            candidate.metadata = {
                "v2_native_ingest": native,
                "deterministic_ingest_error": (source.metadata_ or {}).get("deterministic_ingest_error"),
            }
        except Exception as exc:
            await self.db.rollback()
            source = await self.db.get(Source, source.id)
            if source is not None:
                source.ingestion_status = IngestionStatus.FAILED
                source.fully_ingested = False
                source.metadata_ = {
                    **(source.metadata_ or {}),
                    "vetting_status": "accepted",
                    "ingest_status": "failed",
                    "error": str(exc),
                }
                await self.db.commit()
            candidate.status = "failed"
            candidate.ingest_status = "failed"
            candidate.rejection_reason = str(exc)
            candidate.source_id = str(source.id) if source else None
            candidate.metadata = {"error": str(exc)}

        self.candidates.append(candidate)
        await self._save_candidate(candidate)
        return {
            "accepted": candidate.status == "ingested",
            "status": candidate.status,
            "source_id": candidate.source_id,
            "source_version_ids": candidate.source_version_ids,
            "ingest_status": candidate.ingest_status,
            "fully_ingested": candidate.fully_ingested,
            "provider_file_count": candidate.provider_file_count,
            "ready_provider_file_count": candidate.ready_provider_file_count,
            "url": candidate.normalized_url or candidate.url,
            "gap_ids": candidate.gap_ids,
            "reason": candidate.rejection_reason,
        }

    async def reject_source_candidate_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        candidate = SourceAcquisitionCandidate(
            url=str(args.get("url") or ""),
            normalized_url=normalize_candidate_url(str(args.get("url") or "")),
            gap_ids=[str(item) for item in args.get("gap_ids") or [] if item],
            rationale=str(args.get("reason") or ""),
            confidence=float(args.get("confidence") or 0.0),
            status="rejected",
            rejection_reason=str(args.get("reason") or "Rejected by acquisition agent."),
            ingest_status="rejected",
        )
        self.candidates.append(candidate)
        await self._save_candidate(candidate)
        return _candidate_tool_payload(candidate, accepted=True)

    async def finalize_source_acquisition_round_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        self.finalized_summary = str(args.get("summary") or "")
        unresolved = [str(item) for item in args.get("unresolved_gap_ids") or [] if item]
        return {
            "accepted": True,
            "summary": self.finalized_summary,
            "unresolved_gap_ids": unresolved,
            "candidate_count": len(self.candidates),
            "ingested_count": len([item for item in self.candidates if item.status == "ingested"]),
        }

    def round_artifact_payload(self, *, trigger_phase: str, gap_report_id: str | None = None) -> SourceAcquisitionRound:
        gap_ids = {gap.gap_id for gap in self.gap_report.gaps}
        covered = {gap_id for item in self.candidates if item.status == "ingested" for gap_id in item.gap_ids}
        return SourceAcquisitionRound(
            round_number=self.round_number,
            trigger_phase=trigger_phase,
            gap_report_id=gap_report_id,
            accepted_count=len([item for item in self.candidates if item.status == "ingested"]),
            rejected_count=len([item for item in self.candidates if item.status == "rejected"]),
            failed_count=len([item for item in self.candidates if item.status == "failed"]),
            duplicate_count=len([item for item in self.candidates if item.status == "duplicate"]),
            ingested_source_ids=[item.source_id for item in self.candidates if item.source_id and item.status == "ingested"],
            candidate_urls=[item.normalized_url or item.url for item in self.candidates],
            unresolved_gap_ids=sorted(gap_ids - covered),
            summary=self.finalized_summary or f"Source acquisition round {self.round_number} completed.",
        )

    async def _validation_rejection(self, candidate: SourceAcquisitionCandidate) -> tuple[str, str] | None:
        return await validation_rejection(self, candidate)

    async def _domain_cap_reached(self, candidate: SourceAcquisitionCandidate) -> bool:
        return await domain_cap_reached(
            self.db,
            candidate,
            candidates=self.candidates,
            project_id=self.run.project_id,
            generation_run_id=self.run.id,
        )

    async def _save_candidate(self, candidate: SourceAcquisitionCandidate) -> None:
        key = f"round-{self.round_number}:{_short_hash(candidate.normalized_url or candidate.url)}"
        await save_artifact(
            self.db,
            generation_run_id=self.run.id,
            artifact_type=V2ArtifactType.SOURCE_ACQUISITION_CANDIDATE,
            artifact_key=key,
            payload=candidate,
            status=V2ArtifactStatus.FAILED if candidate.status == "failed" else V2ArtifactStatus.PASSED,
        )
        await self.db.flush()


def _candidate_tool_payload(candidate: SourceAcquisitionCandidate, *, accepted: bool) -> dict[str, Any]:
    return {
        "accepted": accepted,
        "status": candidate.status,
        "url": candidate.normalized_url or candidate.url,
        "gap_ids": candidate.gap_ids,
        "source_id": candidate.source_id,
        "source_version_ids": candidate.source_version_ids[:8],
        "ingest_status": candidate.ingest_status,
        "fully_ingested": candidate.fully_ingested,
        "provider_file_count": candidate.provider_file_count,
        "ready_provider_file_count": candidate.ready_provider_file_count,
        "reason": candidate.rejection_reason,
    }


__all__ = ["SourceAcquisitionToolset", "existing_source_for_url"]
