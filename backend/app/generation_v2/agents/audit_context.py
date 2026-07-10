from __future__ import annotations

from typing import Any

from app.generation_v2.agents.editorial_context import (
    source_ref_context as _source_ref_context,
    trim as _trim,
)
from app.generation_v2.agents.audit_evidence_context import (
    audit_evidence_previews,
    audit_tools_with_evidence_refs,
    evidence_preview_snippet,
    evidence_support_text,
    strip_audit_backend_ids,
)
from app.generation_v2.agents.treatment_context import source_refs_for_ids as _source_refs_for_ids
from app.generation_v2.research_workspace import source_map_research_summary
from app.generation_v2.schemas import ResearchAudit, ResearchMemo, SourceMap
from app.models.generation_run import GenerationRun


def research_audit_input(
    *,
    run: GenerationRun,
    research_memo: ResearchMemo,
    source_map: SourceMap,
    source_inventory: SourceMap | None,
    workspace: dict[str, Any] | None,
    evidence_records: list[dict] | None = None,
) -> dict[str, Any]:
    inventory = source_inventory or source_map
    context = _source_ref_context(inventory.source_cards)
    workspace = workspace or {}
    source_titles_by_id = {card.source_id: card.title for card in inventory.source_cards}
    evidence_previews_by_atom_ref, _, _ = audit_evidence_previews(
        research_memo=research_memo,
        evidence_records=evidence_records or [],
        context=context,
        source_titles_by_id=source_titles_by_id,
    )
    atom_drafts = [item for item in workspace.get("atom_drafts") or [] if isinstance(item, dict)]
    active_drafts = [item for item in atom_drafts if not item.get("merged_into")]
    return {
        "topic": str((run.config or {}).get("user_prompt") or "")[:700],
        "language": str((run.config or {}).get("language") or "en"),
        "duration_minutes": int((run.config or {}).get("duration_target", 1800)) // 60,
        "research_memo": {
            "title": research_memo.title,
            "supported_answer": _trim(research_memo.thesis, 500),
            "editorial_atoms": [
                {
                    "atom_ref": f"A{idx:02d}",
                    "kind": atom.kind,
                    "title": atom.title,
                    "explanation": _trim(atom.explanation, 900),
                    "story_value": _trim(atom.story_value, 600),
                    "source_refs": _source_refs_for_ids(atom.source_ids, context),
                    "quote_refs": atom.quote_refs[:8],
                    "evidence_previews": evidence_previews_by_atom_ref.get(f"A{idx:02d}", []),
                    "confidence": atom.confidence,
                    "risk": atom.risk,
                    "source_coverage_status": atom.source_coverage_status,
                    "story_weight": atom.story_weight,
                    "treatment_role_hint": atom.treatment_role_hint,
                }
                for idx, atom in enumerate(research_memo.editorial_atoms, start=1)
            ],
            "source_coverage": [_trim(item, 450) for item in research_memo.source_coverage[:30]],
            "open_questions": [_trim(item, 450) for item in research_memo.open_questions[:20]],
            "risk_register": [_trim(item, 450) for item in research_memo.risk_register[:30]],
        },
        "workspace": {
            "status": workspace.get("status"),
            "job_count": len(workspace.get("investigation_jobs") or []),
            "processed_jobs": workspace.get("processed_jobs") or [],
            "covered_source_refs": workspace.get("covered_source_refs") or [],
            "selected_evidence_count": len(workspace.get("selected_evidence_ids") or []),
            "active_atom_draft_count": len(active_drafts),
            "merged_atom_draft_count": len(atom_drafts) - len(active_drafts),
            "research_gaps": [
                {
                    "gap_id": item.get("gap_id"),
                    "question": _trim(str(item.get("question") or ""), 450),
                    "category": item.get("category"),
                    "severity": item.get("severity"),
                    "treatment_relevance": item.get("treatment_relevance") or "supporting",
                    "status": item.get("status"),
                    "source_refs": item.get("source_refs") or [],
                    "evidence_count": len(item.get("evidence_ids") or []),
                    "has_evidence": bool(item.get("evidence_ids") or []),
                    "resolution_note": _trim(str(item.get("resolution_note") or ""), 350),
                }
                for item in (workspace.get("research_gaps") or [])[:40]
                if isinstance(item, dict)
            ],
            "risk_register": [
                {
                    "risk": _trim(str(item.get("risk") if isinstance(item, dict) else item), 450),
                    "severity": str(item.get("severity") or "medium") if isinstance(item, dict) else "medium",
                    "source_refs": (item.get("source_refs") or []) if isinstance(item, dict) else [],
                    "evidence_count": len(item.get("evidence_ids") or []) if isinstance(item, dict) else 0,
                    "has_evidence": bool(item.get("evidence_ids") or []) if isinstance(item, dict) else False,
                }
                for item in (workspace.get("risk_register") or [])[:40]
            ],
        },
        "source_map_summary": source_map_research_summary(source_map, inventory, include_evidence_ids=False),
    }


def normalize_research_audit(audit: ResearchAudit) -> ResearchAudit:
    if audit.passed:
        audit.blocking = False
        audit.repair_targets = []
        audit.missing_material = []
        audit.required_followups = []
        return audit
    targets = sorted(
        {
            issue.target_step
            for issue in audit.issues
            if issue.target_step != "none" and issue.severity in {"blocking", "major"}
        }
    )
    if targets:
        audit.repair_targets = targets
    audit.blocking = bool(any(issue.severity == "blocking" for issue in audit.issues)) or audit.blocking
    return audit

__all__ = [
    "audit_evidence_previews",
    "audit_tools_with_evidence_refs",
    "evidence_preview_snippet",
    "evidence_support_text",
    "strip_audit_backend_ids",
]
