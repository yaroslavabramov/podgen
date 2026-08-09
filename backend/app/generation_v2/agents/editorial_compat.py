from __future__ import annotations

from app.generation_v2.agents.audit_agent import audit_research_memo as _audit_research_memo_impl
from app.generation_v2.agents.audit_context import (
    audit_evidence_previews as _audit_evidence_previews,
    audit_tools_with_evidence_refs as _audit_tools_with_evidence_refs,
    evidence_preview_snippet as _evidence_preview_snippet,
    evidence_support_text as _evidence_support_text,
    normalize_research_audit as _normalize_research_audit,
    research_audit_input as _research_audit_input,
    strip_audit_backend_ids as _strip_audit_backend_ids,
)
from app.generation_v2.agents.drafting_agents import (
    build_act_dossier,
    critique_episode,
    extract_and_link_claims,
    final_transcript,
    judge_claim_support,
    rewrite_act_draft,
    update_continuity_ledger,
    write_act_draft,
)
from app.generation_v2.agents.editorial_context import (
    evidence_inventory as _evidence_inventory,
    evidence_metadata_flags,
    quote_inventory as _quote_inventory,
    quote_matches_requirements as _quote_matches_requirements,
    source_inventory as _source_inventory,
    source_ref_context as _source_ref_context,
    source_refs,
    triage_inventory,
)
from app.generation_v2.agents.fallbacks import (
    fallback_act_draft as _fallback_act_draft,
    fallback_claims as _fallback_claims,
    fallback_research_memo as _fallback_research_memo,
    fallback_treatment as _fallback_treatment,
    treatment_act_count_range,
)
from app.generation_v2.agents.research_agents import (
    build_research_finalizer_agent,
    build_research_job_agent,
    build_research_planner_agent,
)
from app.generation_v2.agents.research_memo_agent import build_research_memo_ephemeral as _build_research_memo_ephemeral
from app.generation_v2.agents.source_agents import (
    build_source_acquisition_agent,
    build_source_mapper_agent,
    build_source_triage_finalizer_agent,
    build_source_triage_planner_agent,
    build_source_triage_set_agent,
)
from app.generation_v2.agents.tool_sets import RESEARCH_FINALIZER_TOOL_NAMES, RESEARCH_JOB_TOOL_NAMES
from app.generation_v2.agents.treatment_agent import build_treatment as _build_treatment_impl
from app.generation_v2.agents.treatment_context import (
    add_source_caution as _add_source_caution,
    resolve_treatment_source_focus as _resolve_treatment_source_focus,
    risk_for_treatment as _risk_for_treatment,
    source_note_ref_pair as _source_note_ref_pair,
    source_refs_for_ids as _source_refs_for_ids,
    treatment_atom_payload as _treatment_atom_payload,
    treatment_atom_refs as _treatment_atom_refs,
    treatment_audit_constraints as _treatment_audit_constraints,
    treatment_referenced_source_refs as _treatment_referenced_source_refs,
    treatment_relevant_caution as _treatment_relevant_caution,
    treatment_source_cautions as _treatment_source_cautions,
    treatment_source_context as _treatment_source_context,
)
from app.generation_v2.research_workspace import (
    build_research_workspace_tools,
    ensure_research_workspace,
    load_research_workspace,
    save_research_workspace,
    start_research_job,
)

_evidence_metadata_flags = evidence_metadata_flags
_source_refs = source_refs
_triage_inventory = triage_inventory
__all__ = [
    "RESEARCH_FINALIZER_TOOL_NAMES",
    "RESEARCH_JOB_TOOL_NAMES",
    "_add_source_caution",
    "_audit_evidence_previews",
    "_audit_research_memo_impl",
    "_audit_tools_with_evidence_refs",
    "_build_research_memo_ephemeral",
    "_build_treatment_impl",
    "_evidence_inventory",
    "_evidence_metadata_flags",
    "_evidence_preview_snippet",
    "_evidence_support_text",
    "_fallback_act_draft",
    "_fallback_claims",
    "_fallback_research_memo",
    "_fallback_treatment",
    "_normalize_research_audit",
    "_quote_inventory",
    "_quote_matches_requirements",
    "_research_audit_input",
    "_resolve_treatment_source_focus",
    "_risk_for_treatment",
    "_source_inventory",
    "_source_note_ref_pair",
    "_source_ref_context",
    "_source_refs",
    "_source_refs_for_ids",
    "_strip_audit_backend_ids",
    "_treatment_atom_payload",
    "_treatment_atom_refs",
    "_treatment_audit_constraints",
    "_treatment_referenced_source_refs",
    "_treatment_relevant_caution",
    "_treatment_source_cautions",
    "_treatment_source_context",
    "_triage_inventory",
    "build_act_dossier",
    "build_research_finalizer_agent",
    "build_research_job_agent",
    "build_research_planner_agent",
    "build_research_workspace_tools",
    "build_source_acquisition_agent",
    "build_source_mapper_agent",
    "build_source_triage_finalizer_agent",
    "build_source_triage_planner_agent",
    "build_source_triage_set_agent",
    "critique_episode",
    "ensure_research_workspace",
    "extract_and_link_claims",
    "final_transcript",
    "judge_claim_support",
    "load_research_workspace",
    "rewrite_act_draft",
    "save_research_workspace",
    "start_research_job",
    "treatment_act_count_range",
    "update_continuity_ledger",
    "write_act_draft",
]
