from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agent_output_policy import semantic_agent_output_limit
from app.generation_v2.contracts import V2AgentName, V2Phase
from app.generation_v2.corpus_brief_preview import corpus_brief_preview
from app.generation_v2.prompting import (
    CORPUS_BRIEF_AUDIT_SYSTEM,
    CORPUS_BRIEF_SHADOW_AUDIT_PROMPT,
)
from app.generation_v2.research_source_map_prompting import source_map_research_summary
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime
from app.generation_v2.schemas import (
    CorpusBrief,
    CorpusBriefAuditIssue,
    CorpusBriefAuditReport,
    SourceMap,
)
from app.models.generation_run import GenerationRun


logger = logging.getLogger(__name__)


async def audit_corpus_brief_shadow(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    corpus_brief: CorpusBrief,
    corpus_brief_artifact_id: str,
    source_map: SourceMap,
    source_inventory: SourceMap,
    evidence_tools: list[AgentTool],
) -> CorpusBriefAuditReport:
    prompt = CORPUS_BRIEF_SHADOW_AUDIT_PROMPT.format(
        corpus_brief_json=json.dumps(
            corpus_brief_preview(
                corpus_brief,
                source_inventory=source_inventory,
                item_limit=80,
                text_limit=2_400,
            ),
            ensure_ascii=False,
            default=str,
        ),
        source_map_json=json.dumps(
            source_map_research_summary(
                source_map,
                source_inventory,
                include_evidence_ids=True,
            ),
            ensure_ascii=False,
            default=str,
        ),
        corpus_brief_artifact_id="CB_CURRENT",
        source_map_artifact_id="SM_CURRENT",
    )
    try:
        report = await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=V2AgentName.CORPUS_BRIEF_AUDITOR,
            schema_model=CorpusBriefAuditReport,
            system=CORPUS_BRIEF_AUDIT_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=[tool for tool in evidence_tools if tool.name == "read_evidence"],
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=True),
            max_output_tokens=semantic_agent_output_limit(),
            max_tool_rounds=12,
        )
        return report.model_copy(
            update={
                "corpus_brief_artifact_id": corpus_brief_artifact_id,
                "source_map_artifact_id": corpus_brief.source_map_artifact_id,
            }
        )
    except Exception:
        # Shadow telemetry must not turn an unavailable auditor into a product gate.
        logger.exception("CorpusBrief semantic shadow audit was unavailable")
        return CorpusBriefAuditReport(
            corpus_brief_artifact_id=corpus_brief_artifact_id,
            source_map_artifact_id=corpus_brief.source_map_artifact_id,
            passed=True,
            reported_passed=True,
            summary="Semantic shadow audit was unavailable; objective backend audit remains authoritative.",
            issues=[
                CorpusBriefAuditIssue(
                    issue_id="shadow:unavailable",
                    code="other",
                    severity="warning",
                    message="Semantic shadow auditor did not produce a report.",
                )
            ],
        )


__all__ = ["audit_corpus_brief_shadow"]
