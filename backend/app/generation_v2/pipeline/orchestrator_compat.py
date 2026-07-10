from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import (
    extract_and_link_claims,
    judge_claim_support,
)
from app.generation_v2.artifacts import save_artifact
from app.generation_v2.citation_validation import merge_validation_issues, validate_citations
from app.generation_v2.pipeline.artifact_persistence import (
    save_claim_bundle as _save_claim_bundle,
    save_episode_critique_artifact as _save_episode_critique_artifact,
    save_evidence_artifact_map as _save_evidence_artifact_map,
    save_evidence_artifacts as _save_evidence_artifacts,
    save_main_research_audit_artifact as _save_main_research_audit_artifact,
    save_memo_evidence_gate_artifact as _save_memo_evidence_gate_artifact,
    save_research_memo_artifacts as _save_research_memo_artifacts,
    save_support_judgments as _save_support_judgments,
    save_treatment_artifact as _save_treatment_artifact,
    save_treatment_gate_artifact as _save_treatment_gate_artifact,
    validate_and_save_claim_bundle as _validate_and_save_claim_bundle,
)
from app.generation_v2.pipeline.research_gate import (
    gap_needs_source_acquisition,
    research_audit_target_steps,
    source_acquisition_audit_from_research_gaps as _source_acquisition_audit_from_research_gaps,
)
from app.generation_v2.pipeline.research_repair import (
    enqueue_treatment_research_repair_jobs as _enqueue_treatment_research_repair_jobs_impl,
)
from app.generation_v2.pipeline.source_triage_finalization_helpers import (
    combined_triage_completion as _combined_triage_completion,
    materialize_workspace_deferrals_as_gap_traces as _materialize_workspace_deferrals_as_gap_traces,
    save_and_reload_workspace as _save_and_reload_workspace,
    source_triage_unresolved_completion as _source_triage_unresolved_completion,
)
from app.generation_v2.pipeline.source_triage_repair_context import (
    source_triage_repair_context as _source_triage_repair_context,
)
from app.generation_v2.pipeline.source_triage_set_flow import (
    mark_source_triage_set_unfinished as _mark_source_triage_set_unfinished,
    run_source_triage_set as _run_source_triage_set,
)
from app.generation_v2.pipeline.validation import (
    apply_forbidden_phrase_gate as _apply_forbidden_phrase_gate,
    blocking_fact_issues as _blocking_fact_issues,
    dedupe_strings as _dedupe_strings,
    merge_source_cards as _merge_source_cards,
    research_memo_evidence_ids as _research_memo_evidence_ids,
    validate_research_memo_evidence as _validate_research_memo_evidence,
    validate_treatment as _validate_treatment,
)
from app.generation_v2.research_workspace import (
    enqueue_research_repair_jobs,
    load_research_workspace,
    save_research_workspace,
)
from app.generation_v2.schemas import EpisodeCritique
from app.generation_v2.source_corpus import list_evidence_dicts
from app.models.generation_run import GenerationRun


_gap_needs_source_acquisition = gap_needs_source_acquisition
_research_audit_target_steps = research_audit_target_steps

async def _enqueue_treatment_research_repair_jobs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    treatment_gate: EpisodeCritique,
) -> list[str]:
    return await _enqueue_treatment_research_repair_jobs_impl(
        db,
        run=run,
        treatment_gate=treatment_gate,
        load_workspace=load_research_workspace,
        save_workspace=save_research_workspace,
        enqueue_repair_jobs=enqueue_research_repair_jobs,
    )


__all__ = [
    "_apply_forbidden_phrase_gate",
    "_blocking_fact_issues",
    "_combined_triage_completion",
    "_dedupe_strings",
    "_enqueue_treatment_research_repair_jobs",
    "_gap_needs_source_acquisition",
    "_mark_source_triage_set_unfinished",
    "_materialize_workspace_deferrals_as_gap_traces",
    "_merge_source_cards",
    "_research_audit_target_steps",
    "_research_memo_evidence_ids",
    "_run_source_triage_set",
    "_save_and_reload_workspace",
    "_save_claim_bundle",
    "_save_episode_critique_artifact",
    "_save_evidence_artifact_map",
    "_save_evidence_artifacts",
    "_save_main_research_audit_artifact",
    "_save_memo_evidence_gate_artifact",
    "_save_research_memo_artifacts",
    "_save_support_judgments",
    "_save_treatment_artifact",
    "_save_treatment_gate_artifact",
    "_source_acquisition_audit_from_research_gaps",
    "_source_triage_repair_context",
    "_source_triage_unresolved_completion",
    "_validate_and_save_claim_bundle",
    "_validate_research_memo_evidence",
    "_validate_treatment",
    "extract_and_link_claims",
    "enqueue_research_repair_jobs",
    "judge_claim_support",
    "list_evidence_dicts",
    "load_research_workspace",
    "merge_validation_issues",
    "save_artifact",
    "save_research_workspace",
    "validate_citations",
]
