from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
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
    validate_source_acquisition_tool_args,
)
from app.generation_v2.source_corpus_provider_detach import (
    mark_provider_files_inactive,
    stage_provider_files_inactive,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2Artifact, V2ArtifactStatus, V2ArtifactType
from app.models.source import IngestionStatus, Source, SourceType


logger = logging.getLogger(__name__)


@dataclass
class SourceAcquisitionToolset:
    db: AsyncSession
    run: GenerationRun
    gap_report: CorpusGapReport
    round_number: int
    ingested_count: int = 0
    candidates: list[SourceAcquisitionCandidate] = field(default_factory=list)
    reclaimed_stale_source_ids: list[uuid.UUID] = field(default_factory=list)
    finalized_summary: str = ""

    def agent_tools(self) -> list[AgentTool]:
        return source_acquisition_agent_tools(self)

    async def restore_unfinished_round(self) -> None:
        """Reload the durable candidate budget and finish work interrupted by a worker crash."""
        self.candidates = await _persisted_round_candidates(self.db, self.run.id, self.round_number)
        candidates_by_url = {
            normalize_candidate_url(item.normalized_url or item.url): item
            for item in self.candidates
            if normalize_candidate_url(item.normalized_url or item.url)
        }
        result = await self.db.execute(
            select(Source)
            .where(
                Source.project_id == self.run.project_id,
                Source.generation_run_id == self.run.id,
            )
            .order_by(Source.created_at.asc(), Source.id.asc())
        )
        for source in result.scalars():
            metadata = source.metadata_ or {}
            if not metadata.get("auto_acquired_by_v2"):
                continue
            try:
                source_round = int(metadata.get("acquisition_round") or 0)
            except (TypeError, ValueError):
                continue
            if source_round != self.round_number:
                continue
            normalized_url = normalize_candidate_url(source.original_url or "")
            if not normalized_url:
                continue
            candidate = candidates_by_url.get(normalized_url)
            if candidate is None:
                candidate = _candidate_from_staged_source(source)
                self.candidates.append(candidate)
                candidates_by_url[normalized_url] = candidate
            candidate.source_id = str(source.id)
            candidate.normalized_url = normalized_url
            if source.ingestion_status == IngestionStatus.FAILED:
                candidate.status = "failed"
                candidate.ingest_status = "failed"
                candidate.rejection_reason = str(metadata.get("error") or "Source ingestion failed.")
                await self._save_candidate(candidate)
                continue
            if not source.is_temporary:
                await self._record_ingested_candidate(candidate, source)
                await self._save_candidate(candidate)
                continue

            source.metadata_ = {
                **metadata,
                "acquisition_claim_token": str(metadata.get("acquisition_claim_token") or uuid.uuid4()),
                "acquisition_lease_expires_at": _new_acquisition_lease_expiry(),
                "vetting_status": "pending_ingest",
            }
            candidate.status = "accepted"
            candidate.ingest_status = "pending"
            await self._save_candidate(candidate)
            await self._ingest_staged_candidate(source, candidate)
            await self._save_candidate(candidate)

        self.ingested_count = len([item for item in self.candidates if item.status == "ingested"])

    async def ingest_source_candidate_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        semantic_error = validate_source_acquisition_tool_args("ingest_source_candidate", args)
        if semantic_error:
            return semantic_error
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
        budget_error = self._candidate_budget_error(candidate_url=candidate.normalized_url)
        if budget_error:
            return budget_error
        rejection = await self._validation_rejection(candidate)
        await self._stage_reclaimed_cleanup()
        if rejection:
            candidate.status = rejection[0]  # type: ignore[assignment]
            candidate.rejection_reason = rejection[1]
            candidate.ingest_status = "rejected"
            existing = self._candidate_for_url(candidate.normalized_url)
            if candidate.status == "duplicate" and existing is not None and existing.status == "ingested":
                await self._finish_reclaimed_cleanup()
                return _candidate_tool_payload(candidate, accepted=False)
            self._remember_candidate(candidate)
            await self._save_candidate(candidate)
            await self._finish_reclaimed_cleanup()
            return _candidate_tool_payload(candidate, accepted=False)

        try:
            source_title = _title_from_url(candidate.normalized_url)
        except ValueError as exc:
            candidate.status = "rejected"
            candidate.rejection_reason = str(exc)
            candidate.ingest_status = "rejected"
            self._remember_candidate(candidate)
            await self._save_candidate(candidate)
            await self._finish_reclaimed_cleanup()
            return _candidate_tool_payload(candidate, accepted=False)

        source = Source(
            id=uuid.uuid4(),
            project_id=self.run.project_id,
            source_type=SourceType.WEB,
            original_url=candidate.normalized_url,
            title=source_title,
            ingestion_status=IngestionStatus.PENDING,
            # Acquisition sources stay outside the visible project corpus until
            # native/deterministic ingest and provider sync have both succeeded.
            is_temporary=True,
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
                "vetting_status": "pending_ingest",
                "acquisition_claim_token": str(uuid.uuid4()),
                "acquisition_lease_expires_at": _new_acquisition_lease_expiry(),
            },
        )
        self.db.add(source)
        candidate.source_id = str(source.id)
        candidate.status = "accepted"
        candidate.ingest_status = "pending"
        # Persist source + pending candidate atomically. A crash from this point
        # leaves resumable work and consumes one slot of the three-candidate cap.
        await self._save_candidate(candidate)
        await self._finish_reclaimed_cleanup(already_committed=True)
        await self._ingest_staged_candidate(source, candidate)
        if candidate.status == "ingested":
            self.ingested_count += 1

        self._remember_candidate(candidate)
        await self._save_candidate(candidate)
        return _candidate_tool_payload(candidate, accepted=candidate.status == "ingested")

    async def reject_source_candidate_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        semantic_error = validate_source_acquisition_tool_args("reject_source_candidate", args)
        if semantic_error:
            return semantic_error
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
        budget_error = self._candidate_budget_error(candidate_url=candidate.normalized_url)
        if budget_error:
            return budget_error
        self._remember_candidate(candidate)
        await self._save_candidate(candidate)
        return _candidate_tool_payload(candidate, accepted=True)

    async def finalize_source_acquisition_round_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        semantic_error = validate_source_acquisition_tool_args("finalize_source_acquisition_round", args)
        if semantic_error:
            return semantic_error
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
        await self.db.commit()

    async def _ingest_staged_candidate(
        self,
        source: Source,
        candidate: SourceAcquisitionCandidate,
    ) -> None:
        source_id = source.id
        try:
            await asyncio.wait_for(
                _native_first_ingest_candidate(self.db, source_id),
                timeout=max(30, int(settings.generation_v2_source_acquisition_source_timeout_seconds)),
            )
            refreshed = await self.db.get(Source, source_id)
            if refreshed is None:
                raise RuntimeError("Source disappeared during acquisition ingest")
            if refreshed.is_temporary:
                raise RuntimeError("Source acquisition ingest completed without promotion")
            await self._record_ingested_candidate(candidate, refreshed)
        except Exception as exc:
            await self.db.rollback()
            try:
                await mark_provider_files_inactive(
                    self.db,
                    source_id=source_id,
                    cleanup_status="acquisition_failed",
                )
            except Exception:
                logger.warning("Failed to detach provider files for rejected acquisition source", exc_info=True)
                await self.db.rollback()
            failed_source = await self.db.get(Source, source_id)
            if failed_source is not None:
                failed_source.is_temporary = True
                failed_source.ingestion_status = IngestionStatus.FAILED
                failed_source.fully_ingested = False
                failed_source.metadata_ = {
                    **(failed_source.metadata_ or {}),
                    "vetting_status": "failed_ingest",
                    "ingest_status": "failed",
                    "error": str(exc),
                }
                await self.db.commit()
            candidate.status = "failed"
            candidate.ingest_status = "failed"
            candidate.rejection_reason = str(exc)
            candidate.source_id = str(failed_source.id) if failed_source else str(source_id)
            candidate.metadata = {"error": str(exc)}

    async def _record_ingested_candidate(
        self,
        candidate: SourceAcquisitionCandidate,
        source: Source,
    ) -> None:
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

    async def _stage_reclaimed_cleanup(self) -> None:
        for reclaimed_source_id in self.reclaimed_stale_source_ids:
            await stage_provider_files_inactive(
                self.db,
                source_id=reclaimed_source_id,
                cleanup_reason="stale_acquisition_reclaimed",
            )

    async def _finish_reclaimed_cleanup(self, *, already_committed: bool = False) -> None:
        if not self.reclaimed_stale_source_ids:
            return
        reclaimed_ids = list(self.reclaimed_stale_source_ids)
        self.reclaimed_stale_source_ids.clear()
        if not already_committed:
            await self.db.commit()
        for reclaimed_source_id in reclaimed_ids:
            try:
                await mark_provider_files_inactive(
                    self.db,
                    source_id=reclaimed_source_id,
                    cleanup_status="stale_acquisition_reclaimed",
                )
            except Exception:
                logger.warning("Failed to detach a reclaimed acquisition source", exc_info=True)
                await self.db.rollback()

    def _candidate_for_url(self, url: str) -> SourceAcquisitionCandidate | None:
        normalized_url = normalize_candidate_url(url)
        if not normalized_url:
            return None
        return next(
            (
                item
                for item in self.candidates
                if normalize_candidate_url(item.normalized_url or item.url) == normalized_url
            ),
            None,
        )

    def _remember_candidate(self, candidate: SourceAcquisitionCandidate) -> None:
        normalized_url = normalize_candidate_url(candidate.normalized_url or candidate.url)
        if normalized_url:
            for index, existing in enumerate(self.candidates):
                if normalize_candidate_url(existing.normalized_url or existing.url) == normalized_url:
                    self.candidates[index] = candidate
                    return
        self.candidates.append(candidate)

    def _candidate_budget_error(self, *, candidate_url: str = "") -> dict[str, Any] | None:
        budget = min(3, max(1, int(settings.generation_v2_source_acquisition_candidates_per_round)))
        # Retrying the same URL is still the same candidate. In particular, a
        # failed ingest must not make that URL permanently un-retryable.
        if candidate_url and self._candidate_for_url(candidate_url) is not None:
            return None
        candidate_count = len(
            {
                normalize_candidate_url(item.normalized_url or item.url) or str(item.url)
                for item in self.candidates
            }
        )
        if candidate_count < budget:
            return None
        return {
            "accepted": False,
            "error": "candidate_budget_exhausted",
            "message": f"Source acquisition is limited to {budget} candidates for this repair round.",
            "candidate_count": candidate_count,
        }


def _candidate_tool_payload(candidate: SourceAcquisitionCandidate, *, accepted: bool) -> dict[str, Any]:
    reason = candidate.rejection_reason
    if candidate.status == "failed":
        # Persist the full exception in the candidate artifact, but do not echo
        # backend/database details into the model-facing tool response.
        reason = "Source ingestion failed. Try another candidate or retry this URL later."
    return {
        "accepted": accepted,
        "status": candidate.status,
        "url": candidate.normalized_url or candidate.url,
        "gap_ids": candidate.gap_ids,
        "ingest_status": candidate.ingest_status,
        "fully_ingested": candidate.fully_ingested,
        "provider_file_count": candidate.provider_file_count,
        "ready_provider_file_count": candidate.ready_provider_file_count,
        "reason": reason,
    }


async def _persisted_round_candidates(
    db: AsyncSession,
    generation_run_id: uuid.UUID,
    round_number: int,
) -> list[SourceAcquisitionCandidate]:
    # Deliberately run/round scoped rather than corpus-attempt scoped: these
    # rows are durable repair state, so an unfinished ingest survives the new
    # corpus attempt created when a worker restarts.
    result = await db.execute(
        select(V2Artifact).where(
            V2Artifact.generation_run_id == generation_run_id,
            V2Artifact.artifact_type == V2ArtifactType.SOURCE_ACQUISITION_CANDIDATE,
            V2Artifact.artifact_key.like(f"round-{round_number}:%"),
            V2Artifact.is_current.is_(True),
        )
    )
    candidates: list[SourceAcquisitionCandidate] = []
    for artifact in result.scalars():
        payload = {key: value for key, value in (artifact.payload or {}).items() if key != "_metadata"}
        try:
            candidate = SourceAcquisitionCandidate.model_validate(payload)
        except ValidationError:
            logger.warning("Ignoring invalid persisted source-acquisition candidate %s", artifact.id)
            continue
        candidates.append(candidate)
    return candidates


def _candidate_from_staged_source(source: Source) -> SourceAcquisitionCandidate:
    metadata = source.metadata_ or {}
    try:
        confidence = float(metadata.get("candidate_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return SourceAcquisitionCandidate(
        url=str(metadata.get("candidate_url") or source.original_url or ""),
        normalized_url=normalize_candidate_url(source.original_url or ""),
        gap_ids=[str(item) for item in metadata.get("gap_ids") or [] if item],
        expected_editorial_function=str(metadata.get("expected_editorial_function") or ""),
        rationale=str(metadata.get("candidate_rationale") or "Recovered interrupted acquisition candidate."),
        confidence=min(1.0, max(0.0, confidence)),
        source_type_hint=str(metadata.get("source_type_hint") or "web"),
        authority_hint=str(metadata.get("authority_hint") or "unknown"),
        status="accepted",
        source_id=str(source.id),
        ingest_status=str(source.ingestion_status),
        fully_ingested=bool(source.fully_ingested),
    )


def _new_acquisition_lease_expiry() -> str:
    return (
        datetime.now(timezone.utc)
        + timedelta(
            seconds=max(
                30,
                int(settings.generation_v2_source_acquisition_source_timeout_seconds),
            )
            + 60
        )
    ).isoformat()


__all__ = ["SourceAcquisitionToolset", "existing_source_for_url"]
