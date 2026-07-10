from __future__ import annotations

from app.generation_v2.contracts import V2Phase
import json

from sqlalchemy.ext.asyncio import AsyncSession

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
    prompt = SOURCE_ACQUISITION_PROMPT.format(
        user_prompt=(run.config or {}).get("user_prompt", ""),
        duration_minutes=int((run.config or {}).get("duration_target", 1800)) // 60,
        language=(run.config or {}).get("language", "en"),
        gap_report_json=gap_report.model_dump_json(),
        source_inventory_json=_json(
            _source_inventory(
                (source_inventory or SourceMap()).source_cards,
                limit=120,
                ref_context=source_ref_context,
            )
        ),
        candidate_budget=settings.generation_v2_source_acquisition_candidates_per_round,
        ingest_budget=settings.generation_v2_source_acquisition_max_ingest_per_round,
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
            max_output_tokens=8000,
            max_tool_rounds=settings.generation_v2_source_acquisition_candidates_per_round + 2,
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


__all__ = ["build_source_acquisition_agent"]
