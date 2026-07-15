from __future__ import annotations

from app.generation_v2.contracts import V2Phase
import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agent_output_policy import semantic_agent_output_limit
from app.config import settings
from app.generation_v2.agents.editorial_context import (
    source_inventory as _source_inventory,
    source_ref_context as _source_ref_context,
)
from app.generation_v2.agents.fallbacks import allow_runtime_fallback as _allow_runtime_fallback
from app.generation_v2.agents.tool_sets import ACQUISITION_TOOL_NAMES, phase_tools
from app.generation_v2.prompting import SOURCE_ACQUISITION_PROMPT, SOURCE_ACQUISITION_SYSTEM
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import CorpusGapReport, SourceAcquisitionCompletion, SourceMap
from app.models.generation_run import GenerationRun


async def build_source_acquisition_agent(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    gap_report: CorpusGapReport,
    source_inventory: SourceMap | None,
    acquisition_tools: list[AgentTool] | None = None,
    round_number: int,
) -> SourceAcquisitionCompletion:
    source_ref_context = _source_ref_context((source_inventory or SourceMap()).source_cards)
    candidate_budget = min(3, max(1, int(settings.generation_v2_source_acquisition_candidates_per_round)))
    ingest_budget = min(candidate_budget, max(1, int(settings.generation_v2_source_acquisition_max_ingest_per_round)))
    prompt = SOURCE_ACQUISITION_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        gap_report_json=_json(_model_gap_report(gap_report, source_ref_context)),
        source_inventory_json=_json(
            _model_source_inventory(source_inventory or SourceMap(), source_ref_context)
        ),
        candidate_budget=candidate_budget,
        ingest_budget=ingest_budget,
    )
    try:
        return await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.SOURCE_ACQUISITION,
            agent_name=f"source_acquisition_round_{round_number}",
            schema_model=SourceAcquisitionCompletion,
            system=SOURCE_ACQUISITION_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=phase_tools(acquisition_tools, ACQUISITION_TOOL_NAMES),
            tool_policy=ToolPolicy(file_search=False, web_search=True, local_tools=True),
            max_output_tokens=semantic_agent_output_limit(),
            max_tool_rounds=candidate_budget + 2,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_source_acquisition", f"source_acquisition_round_{round_number}", exc)
        return SourceAcquisitionCompletion(
            ready=False,
            summary="Runtime fallback was used; source acquisition did not run.",
            searched_gap_ids=[gap.gap_id for gap in gap_report.gaps],
            notes=["Source acquisition requires OpenAI web_search and is disabled in runtime fallback."],
        )


def _json(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(
            [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value],
            ensure_ascii=False,
        )
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


_UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)


def _model_gap_report(gap_report: CorpusGapReport, source_ref_context) -> dict:
    return {
        "round_number": gap_report.round_number,
        "trigger_phase": gap_report.trigger_phase,
        "target_duration_seconds": gap_report.target_duration_seconds,
        "summary": _sanitize_model_text(gap_report.summary, source_ref_context),
        "gaps": [
            {
                "gap_id": gap.gap_id,
                "kind": gap.kind,
                "description": (
                    "Source map references sources or evidence unavailable in the current run."
                    if "Source map contains invalid ids:" in gap.description
                    else _sanitize_model_text(gap.description, source_ref_context)
                ),
                "required_editorial_functions": gap.required_editorial_functions,
                "related_source_refs": list(
                    dict.fromkeys(
                        [
                            source_ref_context.ref_by_source_id[source_id]
                            for source_id in gap.related_source_ids
                            if source_id in source_ref_context.ref_by_source_id
                        ]
                        + [
                            source_ref
                            for source_ref in gap.related_source_refs
                            if source_ref in source_ref_context.source_id_by_ref
                        ]
                    )
                ),
                "severity": gap.severity,
                "origin": gap.origin,
                **(
                    {
                        "question_id": _sanitize_model_text(gap.question_id, source_ref_context),
                        "question": _sanitize_model_text(gap.question, source_ref_context),
                        "reason": _sanitize_model_text(gap.reason, source_ref_context),
                    }
                    if gap.question_id
                    else {}
                ),
            }
            for gap in gap_report.gaps
        ],
        "required_editorial_functions": gap_report.required_editorial_functions,
        "source_count": gap_report.source_count,
        "evidence_count": gap_report.evidence_count,
        "should_acquire": gap_report.should_acquire,
    }


def _model_source_inventory(source_inventory: SourceMap, source_ref_context) -> list[dict]:
    inventory = _source_inventory(
        source_inventory.source_cards,
        limit=120,
        ref_context=source_ref_context,
    )
    return [
        _sanitize_model_value(
            {key: value for key, value in item.items() if key != "evidence_ids"},
            source_ref_context,
        )
        for item in inventory
    ]


def _sanitize_model_value(value: object, source_ref_context):
    if isinstance(value, str):
        return _sanitize_model_text(value, source_ref_context)
    if isinstance(value, list):
        return [_sanitize_model_value(item, source_ref_context) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_model_value(item, source_ref_context)
            for key, item in value.items()
        }
    return value


def _sanitize_model_text(value: object, source_ref_context) -> str:
    text = str(value or "")
    for source_id, source_ref in sorted(
        source_ref_context.ref_by_source_id.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        text = text.replace(source_id, source_ref)
    return _UUID_PATTERN.sub("[internal reference omitted]", text)


__all__ = ["build_source_acquisition_agent"]
