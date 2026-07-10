from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents import editorial_compat as _editorial_compat
from app.generation_v2.agents.audit_agent import audit_research_memo as _audit_research_memo_impl
from app.generation_v2.agents.research_memo_agent import build_research_memo as _build_research_memo_impl
from app.generation_v2.agents.treatment_agent import build_treatment as _build_treatment_impl
from app.generation_v2.runtime import AgentTool, V2Runtime
from app.generation_v2.schemas import (
    QuoteCard,
    ResearchAudit,
    ResearchMemo,
    ShowBibleContext,
    SourceCard,
    SourceMap,
    Treatment,
)
from app.generation_v2.tools.evidence_toolset import EvidenceToolset
from app.models.generation_run import GenerationRun

__all__ = ["audit_research_memo", "build_research_memo", "build_treatment"]


def __getattr__(name: str):
    if name in _editorial_compat.__all__:
        return getattr(_editorial_compat, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def build_research_memo(
    db: AsyncSession,
    run: GenerationRun,
    runtime: V2Runtime,
    *,
    source_cards: list[SourceCard],
    quote_cards: list[QuoteCard],
    source_map: SourceMap | None = None,
    source_inventory: SourceMap | None = None,
    evidence_tools: list[AgentTool] | None = None,
    evidence_toolset: EvidenceToolset | None = None,
    vector_store_id: str | None = None,
    show_bible: ShowBibleContext | None = None,
    hosted_file_search_handler=None,
    run_planner: bool = True,
) -> ResearchMemo:
    return await _build_research_memo_impl(
        db,
        run,
        runtime,
        source_cards=source_cards,
        quote_cards=quote_cards,
        source_map=source_map,
        source_inventory=source_inventory,
        evidence_tools=evidence_tools,
        evidence_toolset=evidence_toolset,
        vector_store_id=vector_store_id,
        show_bible=show_bible,
        hosted_file_search_handler=hosted_file_search_handler,
        run_planner=run_planner,
    )


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
    return await _audit_research_memo_impl(
        db,
        run,
        runtime,
        research_memo=research_memo,
        source_map=source_map,
        source_inventory=source_inventory,
        evidence_tools=evidence_tools,
        vector_store_id=vector_store_id,
        show_bible=show_bible,
    )


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
    return await _build_treatment_impl(
        db,
        run,
        runtime,
        research_memo=research_memo,
        source_map=source_map,
        source_inventory=source_inventory,
        research_audit=research_audit,
        evidence_tools=evidence_tools,
        vector_store_id=vector_store_id,
        show_bible=show_bible,
    )
