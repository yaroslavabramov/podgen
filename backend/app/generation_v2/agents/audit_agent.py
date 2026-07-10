from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.audit_context import (
    audit_evidence_previews as _audit_evidence_previews,
    audit_tools_with_evidence_refs as _audit_tools_with_evidence_refs,
    normalize_research_audit as _normalize_research_audit,
    research_audit_input as _research_audit_input,
)
from app.generation_v2.agents.editorial_context import source_ref_context as _source_ref_context
from app.generation_v2.agents.fallbacks import allow_runtime_fallback as _allow_runtime_fallback
from app.generation_v2.agents.json_helpers import json_for_prompt as _json
from app.generation_v2.prompting import RESEARCH_AUDIT_PROMPT, RESEARCH_AUDIT_SYSTEM
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import (
    ResearchAudit,
    ResearchAuditIssue,
    ResearchMemo,
    ShowBibleContext,
    SourceMap,
)
from app.generation_v2.research_workspace import load_research_workspace
from app.generation_v2.source_corpus import list_evidence_dicts
from app.models.generation_run import GenerationRun


async def audit_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap | None = None,
    source_inventory: SourceMap | None = None,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> ResearchAudit:
    del vector_store_id
    source_map = source_map or SourceMap()
    workspace = await load_research_workspace(db, run.id) if db is not None else None
    evidence_records = await list_evidence_dicts(db, run.id) if db is not None else []
    inventory = source_inventory or source_map
    context = _source_ref_context(inventory.source_cards)
    source_titles_by_id = {card.source_id: card.title for card in inventory.source_cards}
    _, evidence_id_by_ref, required_evidence_refs = _audit_evidence_previews(
        research_memo=research_memo,
        evidence_records=evidence_records,
        context=context,
        source_titles_by_id=source_titles_by_id,
    )
    audit_read_refs: set[str] = set()
    prompt = RESEARCH_AUDIT_PROMPT.format(
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        audit_input_json=_json(
            _research_audit_input(
                run=run,
                research_memo=research_memo,
                source_map=source_map,
                source_inventory=source_inventory,
                workspace=workspace,
                evidence_records=evidence_records,
            )
        ),
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
    )
    try:
        audit = await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.RESEARCH_AUDIT,
            agent_name=V2AgentName.RESEARCH_AUDITOR,
            schema_model=ResearchAudit,
            system=RESEARCH_AUDIT_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=_audit_tools_with_evidence_refs(
                evidence_tools,
                evidence_id_by_ref=evidence_id_by_ref,
                read_refs=audit_read_refs,
            ),
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=5000,
        )
        duration_seconds = int((run.config or {}).get("duration_target", 1800))
        missing_required_reads = sorted(required_evidence_refs - audit_read_refs) if duration_seconds >= 2700 else []
        if audit.passed and missing_required_reads:
            return ResearchAudit(
                passed=False,
                score=min(audit.score, 0.55),
                verdict="Research audit did not read required evidence previews.",
                missing_material=["Audit must read required evidence previews before treatment."],
                required_followups=[f"Read required evidence_refs: {', '.join(missing_required_reads[:12])}"],
                blocking=True,
                issues=[
                    ResearchAuditIssue(
                        issue_type="tool_contract",
                        severity="blocking",
                        target_step="research_audit",
                        description="Audit passed without reading required core evidence previews.",
                        suggested_repair="Rerun research audit and read the required evidence_refs before passing.",
                        evidence_refs=missing_required_reads,
                    )
                ],
                repair_targets=["research_audit"],
            )
        return _normalize_research_audit(audit)
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_research_audit", "research_auditor", exc)
        atom_count = len(research_memo.editorial_atoms)
        return ResearchAudit(
            passed=atom_count >= 3,
            score=0.8 if atom_count >= 3 else 0.45,
            verdict="Fallback audit: enough editorial atoms for drafting." if atom_count >= 3 else "Not enough editorial atoms.",
            missing_material=[] if atom_count >= 3 else ["More concrete source material is needed."],
            required_followups=[],
            blocking=atom_count < 3,
            issues=[]
            if atom_count >= 3
            else [
                ResearchAuditIssue(
                    issue_type="insufficient_material",
                    severity="blocking",
                    target_step="editorial_research",
                    description="More concrete source material is needed.",
                    suggested_repair="Run targeted editorial research before treatment.",
                )
            ],
            repair_targets=[] if atom_count >= 3 else ["editorial_research"],
        )


__all__ = ["audit_research_memo"]
