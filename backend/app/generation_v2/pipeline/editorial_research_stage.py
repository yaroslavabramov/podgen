from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation_v2.agents.editorial import audit_research_memo, build_treatment
from app.generation_v2.contracts.pipeline import V2Phase, V2PipelineStep
from app.generation_v2.pipeline.artifact_persistence import (
    save_main_research_audit_artifact,
    save_treatment_artifact,
    save_treatment_gate_artifact,
)
from app.generation_v2.pipeline.editorial_memo_flow import build_and_validate_research_memo
from app.generation_v2.pipeline.editorial_research_state import (
    EditorialResearchResult,
    editorial_runtime_values,
)
from app.generation_v2.pipeline.research_gate import (
    research_audit_failure_message,
    research_audit_requires_source_acquisition,
    source_acquisition_audit_from_research_gaps,
)
from app.generation_v2.pipeline.research_repair import (
    enqueue_treatment_research_repair_jobs as enqueue_treatment_research_repair_jobs_impl,
)
from app.generation_v2.pipeline.source_triage_flow import _run_source_acquisition_until_triage_ready
from app.generation_v2.pipeline.status import act_step_name, commit_step, start_phase
from app.generation_v2.pipeline.validation import validate_treatment
from app.generation_v2.research_workspace import (
    enqueue_research_repair_jobs,
    load_research_workspace,
    save_research_workspace,
)
from app.generation_v2.schemas import EpisodeCritique, ShowBibleContext
from app.models.generation_run import GenerationRun


async def enqueue_treatment_research_repair_jobs(
    db: AsyncSession,
    *,
    run: GenerationRun,
    treatment_gate: EpisodeCritique,
) -> list[str]:
    return await enqueue_treatment_research_repair_jobs_impl(
        db,
        run=run,
        treatment_gate=treatment_gate,
        load_workspace=load_research_workspace,
        save_workspace=save_research_workspace,
        enqueue_repair_jobs=enqueue_research_repair_jobs,
    )


class EditorialResearchStageRunner:
    def __init__(
        self,
        db: AsyncSession,
        run: GenerationRun,
        runtime: Any,
        *,
        triage_state: Any,
        acquisition_rounds_used: int,
        show_bible: ShowBibleContext,
    ) -> None:
        self.db = db
        self.run = run
        self.runtime = runtime
        self.triage_state = triage_state
        self.acquisition_rounds_used = acquisition_rounds_used
        self.show_bible = show_bible
        self.values = editorial_runtime_values(triage_state)
        self.research_repair_rounds_used = 0

    async def run_stage(self) -> EditorialResearchResult:
        while True:
            memo_pass = await self.build_memo()
            memo = memo_pass.memo
            if not memo_pass.memo_evidence_gate.passed:
                if await self.handle_failed_memo_gate(memo, memo_pass.memo_evidence_gate):
                    continue
                raise AssertionError("unreachable memo gate branch")

            await commit_step(
                self.run,
                self.db,
                V2PipelineStep.EDITORIAL_RESEARCH,
                24,
                next_step=V2PipelineStep.RESEARCH_AUDIT,
            )

            audit, audit_artifact_id = await self.run_audit(memo_pass)
            if not audit.passed:
                if await self.handle_failed_audit(audit):
                    continue
                raise AssertionError("unreachable audit gate branch")

            await commit_step(
                self.run,
                self.db,
                V2PipelineStep.RESEARCH_AUDIT,
                32,
                next_step=V2PipelineStep.TREATMENT,
            )

            treatment_result = await self.run_treatment(memo_pass, audit, audit_artifact_id)
            if treatment_result is None:
                continue
            treatment, treatment_artifact_id = treatment_result
            await commit_step(self.run, self.db, V2PipelineStep.TREATMENT, 42, next_step=act_step_name(1))
            return EditorialResearchResult(
                triage_state=self.triage_state,
                acquisition_rounds_used=self.acquisition_rounds_used,
                treatment=treatment,
                treatment_artifact_id=treatment_artifact_id,
                **self.values,
            )

    async def build_memo(self) -> Any:
        await start_phase(self.run, self.db, V2Phase.EDITORIAL_RESEARCH, "Building editorial research memo", 14)
        return await build_and_validate_research_memo(
            self.db,
            self.run,
            self.runtime,
            values=self.values,
            source_inventory=self.triage_state.source_inventory,
            show_bible=self.show_bible,
            run_planner=self.research_repair_rounds_used == 0,
        )

    async def handle_failed_memo_gate(self, memo: Any, memo_evidence_gate: EpisodeCritique) -> bool:
        if memo.open_questions:
            if self.research_repair_rounds_used < 1:
                repair_job_ids = await enqueue_research_repair_jobs(self.db, run=self.run)
                if repair_job_ids:
                    self.research_repair_rounds_used += 1
                    await self.db.commit()
                    return True
            source_acquisition_audit = source_acquisition_audit_from_research_gaps(
                await load_research_workspace(self.db, self.run.id)
            )
            if source_acquisition_audit and await self.try_source_acquisition(
                trigger_phase="research_gap",
                research_audit=source_acquisition_audit,
                reset_repair_rounds=True,
            ):
                return True
            await self.db.commit()
            raise RuntimeError(
                "V2 research memo evidence gate failed after targeted repair: "
                + "; ".join(memo.open_questions[:5])
            )

        if research_audit_requires_source_acquisition(memo_evidence_gate) and await self.try_source_acquisition(
            trigger_phase="memo_evidence",
            research_audit=memo_evidence_gate,
            reset_repair_rounds=True,
        ):
            return True
        await self.db.commit()
        raise RuntimeError(
            research_audit_failure_message(
                memo_evidence_gate,
                label="V2 research memo evidence gate failed",
            )
        )

    async def run_audit(self, memo_pass: Any) -> tuple[EpisodeCritique, str]:
        await start_phase(self.run, self.db, V2Phase.RESEARCH_AUDIT, "Auditing research readiness", 26)
        audit = await audit_research_memo(
            self.db,
            self.run,
            self.runtime,
            research_memo=memo_pass.memo,
            source_map=self.values["source_map"],
            source_inventory=self.triage_state.source_inventory,
            evidence_tools=self.values["evidence_tools"],
            show_bible=self.show_bible,
        )
        audit_artifact_id = await save_main_research_audit_artifact(
            self.db,
            self.run,
            self.runtime,
            audit,
            memo_artifact_id=memo_pass.memo_artifact_id,
            atom_artifact_ids=memo_pass.atom_artifact_ids,
            memo_gate_artifact_id=memo_pass.memo_gate_artifact_id,
            has_vector_store=bool(self.values["corpus"].vector_store_id),
        )
        return audit, audit_artifact_id

    async def handle_failed_audit(self, audit: EpisodeCritique) -> bool:
        if research_audit_requires_source_acquisition(audit) and await self.try_source_acquisition(
            trigger_phase="research_audit",
            research_audit=audit,
            reset_repair_rounds=False,
        ):
            return True
        await self.db.commit()
        raise RuntimeError(research_audit_failure_message(audit))

    async def run_treatment(
        self,
        memo_pass: Any,
        audit: EpisodeCritique,
        audit_artifact_id: str,
    ) -> tuple[Any, str] | None:
        await start_phase(self.run, self.db, V2Phase.TREATMENT, "Creating episode treatment", 34)
        treatment = await build_treatment(
            self.db,
            self.run,
            self.runtime,
            research_memo=memo_pass.memo,
            source_map=self.values["source_map"],
            source_inventory=self.triage_state.source_inventory,
            research_audit=audit,
            evidence_tools=self.values["evidence_tools"],
            show_bible=self.show_bible,
        )
        treatment_gate = validate_treatment(
            treatment,
            self.values["run_source_cards"],
            int((self.run.config or {}).get("duration_target", 1800)),
            research_memo=memo_pass.memo,
        )
        treatment_artifact_id = await save_treatment_artifact(
            self.db,
            self.run,
            self.runtime,
            treatment,
            treatment_gate,
            memo_artifact_id=memo_pass.memo_artifact_id,
            audit_artifact_id=audit_artifact_id,
            atom_artifact_ids=memo_pass.atom_artifact_ids,
            has_vector_store=bool(self.values["corpus"].vector_store_id),
        )
        if not treatment_gate.passed:
            if await self.handle_failed_treatment_gate(treatment_gate, treatment_artifact_id):
                return None
        return treatment, treatment_artifact_id

    async def handle_failed_treatment_gate(self, treatment_gate: EpisodeCritique, treatment_artifact_id: str) -> bool:
        await save_treatment_gate_artifact(
            self.db,
            self.run,
            treatment_gate,
            treatment_artifact_id=treatment_artifact_id,
            source_map_artifact_id=self.values["source_map_artifact_id"],
            source_artifact_ids=self.values["source_artifact_ids"],
        )
        if self.research_repair_rounds_used < 1:
            repair_job_ids = await enqueue_treatment_research_repair_jobs(
                self.db,
                run=self.run,
                treatment_gate=treatment_gate,
            )
            if repair_job_ids:
                self.research_repair_rounds_used += 1
                await self.db.commit()
                return True
        await self.db.commit()
        raise RuntimeError(
            "V2 treatment gate failed: "
            + treatment_gate.summary
            + "; "
            + " | ".join(treatment_gate.weak_sections[:5])
        )

    async def try_source_acquisition(
        self,
        *,
        trigger_phase: str,
        research_audit: EpisodeCritique,
        reset_repair_rounds: bool,
    ) -> bool:
        previous_round = self.acquisition_rounds_used
        self.triage_state, self.acquisition_rounds_used = await _run_source_acquisition_until_triage_ready(
            self.run,
            self.db,
            self.runtime,
            triage_state=self.triage_state,
            trigger_phase=trigger_phase,
            acquisition_rounds_used=self.acquisition_rounds_used,
            research_audit=research_audit,
        )
        if (
            self.acquisition_rounds_used > previous_round
            and self.triage_state.triage_completion.ready
            and self.triage_state.triage_gate.passed
        ):
            self.values = editorial_runtime_values(self.triage_state)
            if reset_repair_rounds:
                self.research_repair_rounds_used = 0
            return True
        return False


__all__ = ["EditorialResearchStageRunner", "enqueue_treatment_research_repair_jobs"]
