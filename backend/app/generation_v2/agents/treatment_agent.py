from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2Phase
from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.fallbacks import (
    allow_runtime_fallback as _allow_runtime_fallback,
    fallback_act_count as _fallback_act_count,
    fallback_treatment as _fallback_treatment,
    treatment_act_count_range,
)
from app.generation_v2.agents.json_helpers import json_for_prompt as _json
from app.generation_v2.agents.treatment_context import (
    treatment_from_plan as _treatment_from_plan,
    treatment_input as _treatment_input,
)
from app.generation_v2.prompting import TREATMENT_PROMPT, TREATMENT_SYSTEM
from app.generation_v2.runtime import AgentTool, ToolPolicy, V2Runtime, V2RuntimeUnavailable
from app.generation_v2.schemas import ResearchAudit, ResearchMemo, ShowBibleContext, SourceMap, Treatment, TreatmentPlan
from app.models.generation_run import GenerationRun


async def build_treatment(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    research_memo: ResearchMemo,
    source_map: SourceMap | None = None,
    source_inventory: SourceMap | None = None,
    research_audit: ResearchAudit | None = None,
    evidence_tools: list[AgentTool] | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
) -> Treatment:
    del evidence_tools, vector_store_id
    source_map = source_map or SourceMap()
    full_source_inventory = source_inventory or source_map
    act_count_range = treatment_act_count_range(int((run.config or {}).get("duration_target", 1800)))
    prompt = TREATMENT_PROMPT.format(
        show_bible_json=(show_bible or ShowBibleContext()).model_dump_json(),
        min_act_count=act_count_range[0],
        max_act_count=act_count_range[1],
        treatment_input_json=_json(
            _treatment_input(
                run=run,
                research_memo=research_memo,
                source_map=source_map,
                source_inventory=full_source_inventory,
                research_audit=research_audit,
                act_count_range=act_count_range,
            )
        ),
    )
    try:
        treatment_plan = await runtime.call_structured(
            db=db,
            generation_run_id=run.id,
            phase=V2Phase.TREATMENT,
            agent_name=V2AgentName.STORY_EDITOR,
            schema_model=TreatmentPlan,
            system=TREATMENT_SYSTEM,
            prompt=prompt,
            vector_store_id=None,
            local_tools=[],
            tool_policy=ToolPolicy(file_search=False, web_search=False, local_tools=False),
            max_output_tokens=12000,
        )
        return _treatment_from_plan(
            treatment_plan,
            research_memo=research_memo,
            source_map=source_map,
            source_inventory=full_source_inventory,
        )
    except V2RuntimeUnavailable as exc:
        _allow_runtime_fallback(runtime, "v2_treatment", "story_editor", exc)
        return _fallback_treatment(run, research_memo, _fallback_act_count(act_count_range))


__all__ = ["build_treatment"]
