from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.runtime import AgentTool
from app.generation_v2.schemas import (
    CorpusGap,
    CorpusGapReport,
    ResearchAudit,
    SourceAcquisitionCandidate,
    SourceAcquisitionRound,
    SourceAcquisitionReport,
    SourceMap,
    SourceTriageGate,
)
from app.generation_v2.source_corpus import sync_source_to_project_corpus_now
from app.ingestion.router import WebQualityIngestionError, _deactivate_native_ingest_for_web_failure, run_ingestion
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import SourceVersion, V2ArtifactStatus, V2ArtifactType
from app.models.source import IngestionStatus, Source, SourceType


_UNSUPPORTED_EXTENSIONS = {
    ".zip",
    ".rar",
    ".7z",
    ".exe",
    ".dmg",
    ".iso",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
}
_SEARCH_HOSTS = {
    "google.com",
    "www.google.com",
    "bing.com",
    "www.bing.com",
    "duckduckgo.com",
    "search.yahoo.com",
}
_SOCIAL_HOSTS = {
    "x.com",
    "twitter.com",
    "www.twitter.com",
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "linkedin.com",
    "www.linkedin.com",
}


def acquisition_enabled(run: GenerationRun) -> bool:
    config = run.config or {}
    mode = str(config.get("source_acquisition_mode") or settings.generation_v2_source_acquisition_mode or "auto")
    return bool(settings.generation_v2_enable_source_acquisition and mode != "off")


def remaining_acquisition_rounds(used_rounds: int) -> int:
    return max(0, int(settings.generation_v2_source_acquisition_max_rounds) - used_rounds)


def build_corpus_gap_report(
    run: GenerationRun,
    *,
    trigger_phase: str,
    round_number: int,
    source_map: SourceMap | None = None,
    triage_completion_summary: str | None = None,
    triage_ready: bool | None = None,
    triage_gate: SourceTriageGate | None = None,
    research_audit: ResearchAudit | None = None,
    treatment_gate: Any | None = None,
) -> CorpusGapReport:
    config = run.config or {}
    duration = int(config.get("duration_target") or 1800)
    gaps: list[CorpusGap] = []
    research_audit_non_acquisition_only = False

    def add_gap(
        kind: str,
        description: str,
        *,
        functions: list[str] | None = None,
        source_ids: list[str] | None = None,
        severity: str = "medium",
        origin: str = "",
    ) -> None:
        clean = " ".join(str(description or "").split())
        if not clean:
            return
        gap_id = f"{trigger_phase}-{len(gaps) + 1}"
        gaps.append(
            CorpusGap(
                gap_id=gap_id,
                kind=kind,  # type: ignore[arg-type]
                description=clean[:900],
                required_editorial_functions=functions or [],
                related_source_ids=source_ids or [],
                severity=severity,  # type: ignore[arg-type]
                origin=origin or trigger_phase,
            )
        )

    if triage_ready is False:
        add_gap(
            "triage_not_ready",
            triage_completion_summary or "Source triage reported that the corpus is not ready.",
            severity="high",
            origin="source_triage_completion",
        )
    if source_map:
        for gap in source_map.gaps[:20]:
            add_gap("source_map_gap", gap, functions=_functions_from_text(gap), severity="medium", origin="source_map")
        for contradiction in source_map.contradictions[:12]:
            add_gap(
                "source_map_gap",
                f"Contradiction needs better coverage: {contradiction}",
                functions=["contradiction", "tension"],
                severity="medium",
                origin="source_map",
            )
    if triage_gate and not triage_gate.passed:
        for function in triage_gate.missing_function_evidence[:20]:
            add_gap(
                "missing_function",
                f"No selected evidence for editorial function: {function}.",
                functions=[function],
                severity="high" if function != "top_evidence_by_function" else "medium",
                origin="source_triage_gate",
            )
        for source_id in triage_gate.missing_source_card_evidence_source_ids[:20]:
            add_gap(
                "coverage",
                f"Source card has no selected evidence for source_id {source_id}.",
                source_ids=[source_id],
                severity="medium",
                origin="source_triage_gate",
            )
        if triage_gate.invalid_source_ids or triage_gate.invalid_evidence_ids:
            add_gap(
                "coverage",
                f"Source map contains invalid ids: sources={triage_gate.invalid_source_ids}, evidence={triage_gate.invalid_evidence_ids}.",
                severity="high",
                origin="source_triage_gate",
            )
    if research_audit and not research_audit.passed:
        if research_audit.issues:
            acquisition_issues = [issue for issue in research_audit.issues if issue.target_step == "source_acquisition"]
            research_audit_non_acquisition_only = not acquisition_issues
            for issue in acquisition_issues[:24]:
                description = issue.suggested_repair or issue.description
                add_gap(
                    "research_audit",
                    description,
                    functions=_functions_from_text(description),
                    severity="high" if issue.severity in {"blocking", "major"} else "medium",
                    origin="research_audit",
                )
        else:
            for item in [*research_audit.missing_material, *research_audit.required_followups][:24]:
                add_gap("research_audit", item, functions=_functions_from_text(item), severity="high", origin="research_audit")
    if treatment_gate and not getattr(treatment_gate, "passed", True):
        for item in [*getattr(treatment_gate, "weak_sections", []), *getattr(treatment_gate, "rewrite_instructions", [])][:24]:
            add_gap("treatment", item, functions=_functions_from_text(item), severity="high", origin="treatment_gate")
    if duration >= 3600 and source_map and len(source_map.evidence_ids) < 12:
        add_gap(
            "duration",
            "Long-form target needs broader source density before drafting.",
            functions=["tension", "mechanism", "change", "impact"],
            severity="high",
            origin="duration_gate",
        )
    if not gaps and not research_audit_non_acquisition_only:
        add_gap("coverage", "Corpus coverage is weak or unspecified; acquire stronger primary and source-backed material.", severity="medium")

    required_functions = sorted({function for gap in gaps for function in gap.required_editorial_functions if function})
    source_count = len(source_map.source_cards) if source_map else 0
    evidence_count = len(source_map.evidence_ids) if source_map else 0
    should_acquire = bool(gaps)
    return CorpusGapReport(
        run_id=str(run.id),
        project_id=str(run.project_id),
        round_number=round_number,
        trigger_phase=trigger_phase,
        target_duration_seconds=duration,
        summary=(
            f"{len(gaps)} corpus gap(s) require source acquisition before continuing."
            if should_acquire
            else "Research audit failure does not require source acquisition."
        ),
        gaps=gaps,
        required_editorial_functions=required_functions,
        source_count=source_count,
        evidence_count=evidence_count,
        should_acquire=should_acquire,
    )


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
        return [
            AgentTool(
                name="ingest_source_candidate",
                description=(
                    "Add one vetted URL to the project corpus for the current gap report. "
                    "Use only after web_search discovers a URL worth keeping. The backend validates the URL, dedupes it, creates a Source, runs native-first ingest, syncs source versions/provider files, and returns source/version ids. "
                    "web_search content is discovery only; final evidence must come from the ingested source in later file_search/source-triage phases."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "gap_ids": {"type": "array", "items": {"type": "string"}},
                        "expected_editorial_function": {"type": "string"},
                        "rationale": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "source_type_hint": {"type": "string"},
                        "authority_hint": {"type": "string"},
                    },
                    "required": [
                        "url",
                        "gap_ids",
                        "expected_editorial_function",
                        "rationale",
                        "confidence",
                        "source_type_hint",
                        "authority_hint",
                    ],
                },
                handler=self.ingest_source_candidate_tool,
            ),
            AgentTool(
                name="reject_source_candidate",
                description=(
                    "Record an explicit rejection for a URL found during web_search. Use this for duplicates, weak relevance, unsupported file types, search pages, social posts, SEO summaries, or low-confidence candidates."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "gap_ids": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["url", "gap_ids", "reason", "confidence"],
                },
                handler=self.reject_source_candidate_tool,
            ),
            AgentTool(
                name="finalize_source_acquisition_round",
                description=(
                    "Finish this acquisition round after candidate discovery and ingest calls are complete. Include unresolved gap ids so the backend can decide whether to retry or fail closed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "unresolved_gap_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["summary", "unresolved_gap_ids"],
                },
                handler=self.finalize_source_acquisition_round_tool,
            ),
        ]

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
        if self.ingested_count >= max(0, int(settings.generation_v2_source_acquisition_max_ingest_per_round)):
            return "rejected", "Round ingest budget is exhausted."
        reason = validate_candidate_static(candidate, self.gap_report)
        if reason:
            return "rejected", reason
        duplicate_source = await existing_source_for_url(self.db, self.run.project_id, candidate.normalized_url)
        if duplicate_source is not None:
            return "duplicate", f"URL is already in project corpus as source_id {duplicate_source.id}."
        if await self._domain_cap_reached(candidate):
            return "rejected", "Same-domain acquisition cap reached for this round/run."
        return None

    async def _domain_cap_reached(self, candidate: SourceAcquisitionCandidate) -> bool:
        if _is_primaryish(candidate):
            return False
        domain = urlparse(candidate.normalized_url).hostname or ""
        if not domain:
            return False
        round_count = sum(1 for item in self.candidates if urlparse(item.normalized_url or item.url).hostname == domain and item.status == "ingested")
        if round_count >= max(1, int(settings.generation_v2_source_acquisition_domain_cap)):
            return True
        result = await self.db.execute(
            select(Source).where(
                Source.project_id == self.run.project_id,
                Source.generation_run_id == self.run.id,
                Source.original_url.is_not(None),
            )
        )
        total = 0
        for source in result.scalars():
            metadata = source.metadata_ or {}
            if not metadata.get("auto_acquired_by_v2"):
                continue
            if urlparse(source.original_url or "").hostname == domain:
                total += 1
        return total >= max(1, int(settings.generation_v2_source_acquisition_domain_cap))

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


async def existing_source_for_url(db: AsyncSession, project_id: uuid.UUID, normalized_url: str) -> Source | None:
    result = await db.execute(select(Source).where(Source.project_id == project_id, Source.original_url.is_not(None)))
    for source in result.scalars():
        if normalize_candidate_url(source.original_url or "") == normalized_url:
            return source
    return None


def validate_candidate_static(candidate: SourceAcquisitionCandidate, gap_report: CorpusGapReport) -> str | None:
    if not candidate.normalized_url:
        return "Invalid or empty URL."
    parsed = urlparse(candidate.normalized_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "Candidate URL must be http(s)."
    host = (parsed.hostname or "").lower()
    if host in _SEARCH_HOSTS or (host.endswith("google.com") and parsed.path.startswith("/search")):
        return "Search result pages are not source material."
    if host in _SOCIAL_HOSTS:
        return "Social posts are not accepted as source-acquisition corpus material by default."
    path_lower = parsed.path.lower()
    if any(path_lower.endswith(ext) for ext in _UNSUPPORTED_EXTENSIONS):
        return "Unsupported file type for V2 source acquisition."
    allowed_gap_ids = {gap.gap_id for gap in gap_report.gaps}
    unknown_gap_ids = [gap_id for gap_id in candidate.gap_ids if gap_id not in allowed_gap_ids]
    if not candidate.gap_ids or unknown_gap_ids:
        return "Candidate must map to known gap_ids from the current CorpusGapReport."
    if candidate.confidence < float(settings.generation_v2_source_acquisition_min_confidence):
        return "Candidate confidence is below the acquisition threshold."
    if not candidate.rationale.strip():
        return "Candidate rationale is required."
    return None


def normalize_candidate_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    query = urlencode(query_items, doseq=True)
    return urlunparse((scheme, netloc, path.rstrip("/") or "/", "", query, ""))


def _functions_from_text(text: str) -> list[str]:
    lowered = text.lower()
    mapping = {
        "tension": ["conflict", "contradiction", "dispute", "tension", "tradeoff", "trade-off"],
        "mechanism": ["mechanism", "how", "why", "process", "system"],
        "change": ["turn", "turning", "shift", "change", "reform", "reversal"],
        "impact": ["consequence", "impact", "effect", "result", "cost", "harm", "benefit", "risk"],
        "driver": ["driver", "cause", "pressure", "constraint", "incentive", "condition"],
        "context": ["history", "background", "origin", "setup"],
    }
    return [function for function, markers in mapping.items() if any(marker in lowered for marker in markers)]


async def _native_first_ingest_candidate(db: AsyncSession, source_id: uuid.UUID) -> None:
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found")
    sync_summary = await sync_source_to_project_corpus_now(db, source)
    await db.commit()
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError("Source not found after V2 native sync")
    try:
        source.ingestion_status = IngestionStatus.PROCESSING
        await db.commit()
        await run_ingestion(source, db)
        source.ingestion_status = IngestionStatus.COMPLETED
        source.fully_ingested = True
        metadata = dict(source.metadata_ or {})
        metadata.pop("error", None)
        metadata["ingest_status"] = "completed"
        source.metadata_ = metadata
        await db.commit()
        await sync_source_to_project_corpus_now(db, source)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        source = await db.get(Source, source_id)
        if source is None:
            raise
        if isinstance(exc, WebQualityIngestionError) or getattr(exc, "invalidate_native_ingest", False):
            await _deactivate_native_ingest_for_web_failure(db, source, str(exc))
            metadata = dict(source.metadata_ or {})
            metadata.update(getattr(exc, "metadata_patch", lambda: {})())
            metadata["error"] = str(exc)
            metadata["deterministic_ingest_error"] = str(exc)
            metadata["ingest_status"] = "failed_invalid_web_source"
            source.metadata_ = metadata
            source.ingestion_status = IngestionStatus.FAILED
            source.fully_ingested = False
            await db.commit()
            raise
        if _native_ready(source.metadata_, sync_summary):
            metadata = dict(source.metadata_ or {})
            metadata.pop("error", None)
            metadata["deterministic_ingest_error"] = str(exc)
            metadata["ingest_status"] = "completed_v2_native_parser_failed"
            source.metadata_ = metadata
            source.ingestion_status = IngestionStatus.COMPLETED
            source.fully_ingested = False
            await db.commit()
            return
        source.ingestion_status = IngestionStatus.FAILED
        source.fully_ingested = False
        source.metadata_ = {**(source.metadata_ or {}), "error": str(exc), "ingest_status": "failed"}
        await db.commit()
        raise


def _native_ready(metadata: dict | None, sync_summary: dict[str, Any] | None = None) -> bool:
    native = (metadata or {}).get("v2_native_ingest") if isinstance(metadata, dict) else {}
    if not isinstance(native, dict):
        native = {}
    try:
        ready_provider_count = int(native.get("ready_provider_file_count") or (sync_summary or {}).get("ready_provider_file_count") or 0)
    except (TypeError, ValueError):
        ready_provider_count = 0
    return bool(ready_provider_count > 0 or native.get("openai_url_read_status") in {"readable", "partial"})


async def _source_versions(db: AsyncSession, source_id: uuid.UUID) -> list[SourceVersion]:
    result = await db.execute(select(SourceVersion).where(SourceVersion.source_id == source_id).order_by(SourceVersion.created_at.asc()))
    return list(result.scalars())


def _title_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").split("/")[-1] or parsed.hostname or "Acquired source"
    return path.replace("-", " ").replace("_", " ")[:500]


def _is_primaryish(candidate: SourceAcquisitionCandidate) -> bool:
    hint = f"{candidate.authority_hint} {candidate.normalized_url}".lower()
    return any(marker in hint for marker in ("official", "primary", ".gov", ".edu", "who.int", "sec.gov", "court", "archive"))


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


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
