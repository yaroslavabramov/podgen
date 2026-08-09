from __future__ import annotations

from app.generation_v2.contracts import V2AgentName, V2ToolName
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import build_source_triage_planner_agent
from app.generation_v2.artifacts import save_artifact, save_tool_trace
from app.generation_v2.contracts.pipeline import V2Phase
from app.generation_v2.pipeline.source_triage_artifacts import save_source_triage_artifacts
from app.generation_v2.pipeline.source_triage_finalization_helpers import (
    combined_triage_completion,
)
from app.generation_v2.pipeline.source_triage_source_mapper_flow import (
    SourceMapperOperationalError,
    run_source_mapper_for_set,
)
from app.generation_v2.pipeline.state import TriageState
from app.generation_v2.pipeline.status import start_phase
from app.generation_v2.runtime import V2Runtime
from app.generation_v2.schemas import SourceMap, SourceTriageCompletion
from app.generation_v2.source_corpus import CorpusPreparationResult, prepare_v2_corpus
from app.generation_v2.source_triage_workspace import (
    build_workspace_tools,
    ensure_source_triage_workspace,
    load_source_triage_workspace,
    save_source_triage_workspace,
    unprocessed_source_sets,
    planner_source_catalog,
    workspace_summary_for_prompt,
)
from app.generation_v2.source_triage_repair_state import pending_triage_repair
from app.generation_v2.source_triage_canonical import materialize_source_map_v2
from app.generation_v2.tools.evidence import (
    EvidenceToolset,
    build_source_inventory,
    build_source_map_from_write_actions,
    validate_source_map_gate,
)
from app.models.generation_run import GenerationRun
from app.models.generation_v2 import V2ArtifactStatus, V2ArtifactType
from app.pipeline.step_control import assert_step_not_stopped


@dataclass
class SourceTriageStageContext:
    corpus: CorpusPreparationResult
    evidence_toolset: EvidenceToolset
    evidence_tools: list[Any]
    source_inventory: SourceMap
    workspace: dict[str, Any]


class SourceTriageStageRunner:
    def __init__(
        self,
        run: GenerationRun,
        db: AsyncSession,
        runtime: V2Runtime,
        *,
        prepared_corpus: CorpusPreparationResult | None = None,
    ) -> None:
        self.run = run
        self.db = db
        self.runtime = runtime
        self.prepared_corpus = prepared_corpus

    async def run_stage(self) -> TriageState:
        context = await self.prepare_context()
        planner_completion, set_completions, workspace = await self.run_planner_and_sets(context)
        source_map, finalizer_completion, workspace = await self.materialize_source_mapping(context, workspace)
        return await self.validate_repair_and_persist(
            context,
            workspace=workspace,
            source_map=source_map,
            planner_completion=planner_completion,
            set_completions=set_completions,
            finalizer_completion=finalizer_completion,
        )

    async def prepare_context(self) -> SourceTriageStageContext:
        if self.prepared_corpus is None:
            await start_phase(self.run, self.db, V2Phase.CORPUS_PREPARE, "Preparing evidence corpus", 4)
            corpus = await prepare_v2_corpus(self.db, self.run)
        else:
            corpus = self.prepared_corpus
        await start_phase(self.run, self.db, V2Phase.SOURCE_TRIAGE, "Mapping the prepared source corpus", 8)
        evidence_toolset = EvidenceToolset(
            db=self.db,
            run=self.run,
            run_view=corpus.run_view,
            provider_index=corpus.provider_index,
        )
        evidence_tools = evidence_toolset.agent_tools()
        source_inventory = await build_source_inventory(self.db, corpus.run_view)
        workspace = await ensure_source_triage_workspace(
            self.db,
            run=self.run,
            run_view=corpus.run_view,
            inventory=source_inventory,
        )
        return SourceTriageStageContext(
            corpus=corpus,
            evidence_toolset=evidence_toolset,
            evidence_tools=evidence_tools,
            source_inventory=source_inventory,
            workspace=workspace,
        )

    async def run_planner_and_sets(
        self,
        context: SourceTriageStageContext,
    ) -> tuple[SourceTriageCompletion, list[SourceTriageCompletion], dict[str, Any]]:
        if pending_triage_repair(context.workspace) is not None:
            return (
                SourceTriageCompletion(
                    ready=False,
                    summary="Resuming the durably reserved source-triage repair pass.",
                    notes=[],
                ),
                [],
                context.workspace,
            )
        planner_completion = await self.run_planner(context)
        workspace = await load_source_triage_workspace(self.db, self.run.id) or context.workspace
        mapper_completions: list[SourceTriageCompletion] = []
        if workspace.get("status") != "planned":
            await self.record_planner_not_finalized(workspace, planner_completion)
            return planner_completion, mapper_completions, workspace
        for source_set in unprocessed_source_sets(workspace):
            await assert_step_not_stopped()
            completions = await run_source_mapper_for_set(
                run=self.run,
                db=self.db,
                runtime=self.runtime,
                corpus=context.corpus,
                source_inventory=context.source_inventory,
                workspace=workspace,
                source_set=source_set,
            )
            mapper_completions.extend(completions)
            workspace = await load_source_triage_workspace(self.db, self.run.id) or workspace
            set_id = str(source_set.get("set_id") or "")
            persisted_set = next(
                (item for item in workspace.get("source_sets") or [] if str(item.get("set_id") or "") == set_id),
                None,
            )
            if persisted_set is None or persisted_set.get("status") not in {"mapped", "deferred"}:
                raise SourceMapperOperationalError(
                    f"Source routing set {set_id or '<missing>'} returned without terminal mapped/deferred state."
                )
        return planner_completion, mapper_completions, workspace

    async def run_planner(self, context: SourceTriageStageContext) -> SourceTriageCompletion:
        planner_tools = build_workspace_tools(
            db=self.db,
            run=self.run,
            workspace=context.workspace,
            inventory=context.source_inventory,
            evidence_toolset=context.evidence_toolset,
            mode="planner",
        )
        return await build_source_triage_planner_agent(
            self.db,
            self.run,
            self.runtime,
            workspace_summary=workspace_summary_for_prompt(context.workspace, include_planner_resume=True),
            source_catalog=planner_source_catalog(context.source_inventory),
            workspace_tools=planner_tools,
        )

    async def record_planner_not_finalized(
        self,
        workspace: dict[str, Any],
        planner_completion: SourceTriageCompletion,
    ) -> None:
        await save_tool_trace(
            self.db,
            generation_run_id=self.run.id,
            phase=V2Phase.SOURCE_TRIAGE,
            agent_name=V2AgentName.SOURCE_TRIAGE_PLANNER,
            tool_name=V2ToolName.TRIAGE_WORKSPACE_PLAN_NOT_FINALIZED,
            input_json={"workspace_status": workspace.get("status")},
            output_json={
                "accepted": False,
                "planner_ready": planner_completion.ready,
                "summary": planner_completion.summary,
            },
        )

    async def materialize_source_mapping(
        self,
        context: SourceTriageStageContext,
        workspace: dict[str, Any],
    ) -> tuple[SourceMap, SourceTriageCompletion, dict[str, Any]]:
        source_map = await build_source_map_from_write_actions(
            self.db,
            generation_run_id=self.run.id,
            inventory=context.source_inventory,
            run_view=context.corpus.run_view,
        )
        source_map = materialize_source_map_v2(
            source_map,
            workspace=workspace,
            inventory=context.source_inventory,
        )
        return (
            source_map,
            SourceTriageCompletion(
                ready=True,
                summary="Source mapping materialized deterministically; no triage finalizer agent was run.",
                notes=[],
            ),
            workspace,
        )

    async def validate_repair_and_persist(
        self,
        context: SourceTriageStageContext,
        *,
        workspace: dict[str, Any],
        source_map: SourceMap,
        planner_completion: SourceTriageCompletion,
        set_completions: list[SourceTriageCompletion],
        finalizer_completion: SourceTriageCompletion,
    ) -> TriageState:
        triage_completion = combined_triage_completion(planner_completion, set_completions, finalizer_completion)
        initial_completion_payload = triage_completion.model_dump(mode="json")
        triage_completion_artifact = await save_artifact(
            self.db,
            generation_run_id=self.run.id,
            artifact_type=V2ArtifactType.SOURCE_TRIAGE,
            artifact_key="completion",
            payload=triage_completion,
            provider="openai" if context.corpus.vector_store_id else None,
            model=self.runtime.model_for(V2Phase.SOURCE_TRIAGE, "source_mapper_agent"),
            status=V2ArtifactStatus.PASSED if triage_completion.ready else V2ArtifactStatus.FAILED,
            prompt_hash=self.runtime.prompt_hash_for(V2Phase.SOURCE_TRIAGE, "source_mapper_agent"),
            metadata=self.runtime.fallback_metadata_for(V2Phase.SOURCE_TRIAGE, "source_mapper_agent"),
        )
        triage_gate = await validate_source_map_gate(
            self.db,
            generation_run_id=self.run.id,
            source_map=source_map,
            run_view=context.corpus.run_view,
            triage_completion=triage_completion,
            triage_workspace=workspace,
        )
        if not triage_gate.passed:
            triage_completion.notes.append(
                "Source-mapping gate failed; triage repair agents are not part of the active source-mapping flow."
            )
        final_completion_payload = triage_completion.model_dump(mode="json")
        if final_completion_payload != initial_completion_payload:
            triage_completion_artifact = await save_artifact(
                self.db,
                generation_run_id=self.run.id,
                artifact_type=V2ArtifactType.SOURCE_TRIAGE,
                artifact_key="completion",
                payload=triage_completion,
                provider="openai" if context.corpus.vector_store_id else None,
                model=self.runtime.model_for(V2Phase.SOURCE_TRIAGE, "source_mapper_agent"),
                status=V2ArtifactStatus.PASSED if triage_completion.ready else V2ArtifactStatus.FAILED,
                prompt_hash=self.runtime.prompt_hash_for(
                    V2Phase.SOURCE_TRIAGE,
                    "source_mapper_agent",
                ),
                metadata=self.runtime.fallback_metadata_for(
                    V2Phase.SOURCE_TRIAGE,
                    "source_mapper_agent",
                ),
                parent_artifact_ids=[str(triage_completion_artifact.id)],
            )
        workspace = await load_source_triage_workspace(self.db, self.run.id) or workspace
        workspace_artifact = await save_source_triage_workspace(
            self.db,
            generation_run_id=self.run.id,
            workspace={**workspace, "status": "finalized" if triage_gate.passed else "failed_gate"},
            status=V2ArtifactStatus.PASSED if triage_gate.passed else V2ArtifactStatus.FAILED,
        )
        artifact_set = await save_source_triage_artifacts(
            self.db,
            self.run,
            self.runtime,
            source_map,
            triage_gate,
            triage_completion_artifact_id=str(triage_completion_artifact.id),
            has_vector_store=bool(context.corpus.vector_store_id),
            workspace=workspace,
            run_view=context.corpus.run_view,
        )
        return TriageState(
            corpus=context.corpus,
            evidence_toolset=context.evidence_toolset,
            evidence_tools=context.evidence_tools,
            source_inventory=context.source_inventory,
            triage_completion=triage_completion,
            source_map=source_map,
            triage_gate=triage_gate,
            source_cards=source_map.source_cards,
            quote_cards=source_map.quote_cards,
            source_map_artifact_id=artifact_set.source_map_artifact_id,
            evidence_artifact_ids=artifact_set.evidence_artifact_ids,
            source_artifact_ids=artifact_set.source_artifact_ids,
            quote_artifact_ids=artifact_set.quote_artifact_ids,
            workspace_artifact_id=str(workspace_artifact.id),
            triage_artifact_id=artifact_set.triage_artifact_id,
            triage_gate_artifact_id=artifact_set.triage_gate_artifact_id,
            corpus_brief=artifact_set.corpus_brief,
            corpus_brief_artifact_id=artifact_set.corpus_brief_artifact_id,
            corpus_brief_audit_artifact_id=artifact_set.corpus_brief_audit_artifact_id,
            corpus_brief_shadow_audit_artifact_id=None,
        )


__all__ = ["SourceTriageStageContext", "SourceTriageStageRunner"]
