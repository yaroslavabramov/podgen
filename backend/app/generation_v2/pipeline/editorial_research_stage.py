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
    research_audit_target_steps,
    source_acquisition_audit_from_research_gaps,
    treatment_gate_research_issues,
)
from app.generation_v2.pipeline.research_validation import (
    exact_quote_evidence_ids_for_run,
    selected_evidence_ids_for_run,
)
from app.generation_v2.pipeline.research_repair import (
    enqueue_audit_research_repair_jobs,
    enqueue_treatment_research_repair_jobs as enqueue_treatment_research_repair_jobs_impl,
    prepare_research_finalizer_repair,
)
from app.generation_v2.pipeline.source_triage_flow import _run_source_acquisition_until_triage_ready
from app.generation_v2.pipeline.status import act_step_name, commit_step, start_phase
from app.generation_v2.pipeline.validation import validate_treatment
from app.generation_v2.research_workspace import (
    enqueue_research_repair_jobs,
    load_research_workspace,
    save_research_workspace,
)
from app.generation_v2.research_workspace_repair_budget import (
    AUDIT_RERUN_BUDGET,
    TREATMENT_REWRITE_BUDGET,
    complete_persisted_repair_operation,
    load_persisted_repair_operation,
    reserve_persisted_repair_operation,
    start_persisted_repair_operation,
)
from app.generation_v2.schemas import EpisodeCritique, ShowBibleContext
from app.generation_v2.source_corpus import list_evidence_dicts
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

            rebuild_memo = False
            while True:
                audit, audit_artifact_id = await self.run_audit(memo_pass)
                if audit.passed:
                    break
                repair_action = await self.handle_failed_audit(audit)
                if repair_action == "rerun_audit":
                    continue
                if repair_action == "rebuild_memo":
                    rebuild_memo = True
                    break
                raise AssertionError("unreachable audit gate branch")
            if rebuild_memo:
                continue

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
            run_planner=True,
        )

    async def handle_failed_memo_gate(self, memo: Any, memo_evidence_gate: EpisodeCritique) -> bool:
        if memo.open_questions:
            repair_job_ids = await enqueue_research_repair_jobs(self.db, run=self.run)
            if repair_job_ids:
                await self.db.commit()
                return True
            source_acquisition_audit = source_acquisition_audit_from_research_gaps(
                await load_research_workspace(self.db, self.run.id)
            )
            if source_acquisition_audit and await self.try_source_acquisition(
                trigger_phase="research_gap",
                research_audit=source_acquisition_audit,
            ):
                return True
            await self.db.commit()
            raise RuntimeError(
                "V2 research memo evidence gate failed after targeted repair: "
                + "; ".join(memo.open_questions[:5])
            )

        if "editorial_research" in research_audit_target_steps(memo_evidence_gate):
            repair_job_ids = await enqueue_audit_research_repair_jobs(
                self.db,
                run=self.run,
                audit=memo_evidence_gate,
            )
            if repair_job_ids:
                await self.db.commit()
                return True

        if research_audit_requires_source_acquisition(memo_evidence_gate) and await self.try_source_acquisition(
            trigger_phase="memo_evidence",
            research_audit=memo_evidence_gate,
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
        audit_repair = await start_persisted_repair_operation(
            self.db,
            generation_run_id=self.run.id,
            budget_name=AUDIT_RERUN_BUDGET,
        )
        if audit_repair is not None:
            await self.db.commit()
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
        if audit_repair is not None:
            await complete_persisted_repair_operation(
                self.db,
                generation_run_id=self.run.id,
                budget_name=AUDIT_RERUN_BUDGET,
            )
            await self.db.commit()
        return audit, audit_artifact_id

    async def handle_failed_audit(self, audit: EpisodeCritique) -> str:
        if research_audit_requires_source_acquisition(audit) and await self.try_source_acquisition(
            trigger_phase="research_audit",
            research_audit=audit,
        ):
            return "rebuild_memo"
        targets = research_audit_target_steps(audit)
        if "editorial_research" in targets:
            repair_job_ids = await enqueue_audit_research_repair_jobs(self.db, run=self.run, audit=audit)
            if repair_job_ids:
                await self.db.commit()
                return "rebuild_memo"
        if "research_finalizer" in targets:
            if await prepare_research_finalizer_repair(self.db, run=self.run, audit=audit):
                await self.db.commit()
                return "rebuild_memo"
        if "research_audit" in targets:
            operation = await reserve_persisted_repair_operation(
                self.db,
                generation_run_id=self.run.id,
                budget_name=AUDIT_RERUN_BUDGET,
                operation_kind="research_audit_rerun",
                payload={"audit": audit.model_dump(mode="json")},
            )
            if operation is not None:
                await self.db.commit()
                return "rerun_audit"
        await self.db.commit()
        raise RuntimeError(research_audit_failure_message(audit))

    async def run_treatment(
        self,
        memo_pass: Any,
        audit: EpisodeCritique,
        audit_artifact_id: str,
    ) -> tuple[Any, str] | None:
        await start_phase(self.run, self.db, V2Phase.TREATMENT, "Creating episode treatment", 34)
        run_view = getattr(self.values["corpus"], "run_view", None)
        valid_evidence_ids: set[str] | None = None
        valid_quote_refs: set[str] | None = None
        if run_view is not None:
            evidence_records = await list_evidence_dicts(self.db, self.run.id, run_view=run_view)
            valid_evidence_ids = selected_evidence_ids_for_run(evidence_records, run_view)
            valid_quote_refs = exact_quote_evidence_ids_for_run(evidence_records, run_view)
        pending_rewrite = await load_persisted_repair_operation(
            self.db,
            generation_run_id=self.run.id,
            budget_name=TREATMENT_REWRITE_BUDGET,
            pending_only=True,
        )
        repair_instructions = _repair_instructions_from_operation(pending_rewrite)
        repair_attempt_pending = pending_rewrite is not None
        while True:
            using_repair_attempt = repair_attempt_pending
            if using_repair_attempt:
                started_rewrite = await start_persisted_repair_operation(
                    self.db,
                    generation_run_id=self.run.id,
                    budget_name=TREATMENT_REWRITE_BUDGET,
                )
                if started_rewrite is None:
                    raise RuntimeError("V2 treatment repair reservation disappeared before resume.")
                repair_instructions = _repair_instructions_from_operation(started_rewrite)
                await self.db.commit()
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
                repair_instructions=repair_instructions,
            )
            treatment_gate = validate_treatment(
                treatment,
                self.values["run_source_cards"],
                int((self.run.config or {}).get("duration_target", 1800)),
                research_memo=memo_pass.memo,
                valid_evidence_ids=valid_evidence_ids,
                valid_quote_refs=valid_quote_refs,
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
            if using_repair_attempt:
                await complete_persisted_repair_operation(
                    self.db,
                    generation_run_id=self.run.id,
                    budget_name=TREATMENT_REWRITE_BUDGET,
                )
                await self.db.commit()
            if treatment_gate.passed:
                return treatment, treatment_artifact_id
            if not using_repair_attempt and not treatment_gate_research_issues(treatment_gate):
                await save_treatment_gate_artifact(
                    self.db,
                    self.run,
                    treatment_gate,
                    treatment_artifact_id=treatment_artifact_id,
                    source_map_artifact_id=self.values["source_map_artifact_id"],
                    source_artifact_ids=self.values["source_artifact_ids"],
                )
                next_repair_instructions = list(
                    treatment_gate.rewrite_instructions or treatment_gate.weak_sections
                )
                operation = await reserve_persisted_repair_operation(
                    self.db,
                    generation_run_id=self.run.id,
                    budget_name=TREATMENT_REWRITE_BUDGET,
                    operation_kind="treatment_rewrite",
                    payload={
                        "repair_instructions": next_repair_instructions,
                        "failed_treatment_artifact_id": treatment_artifact_id,
                        "treatment_gate": treatment_gate.model_dump(mode="json"),
                    },
                )
                if operation is not None:
                    await self.db.commit()
                    repair_instructions = _repair_instructions_from_operation(operation)
                    repair_attempt_pending = True
                    continue
            if await self.handle_failed_treatment_gate(treatment_gate, treatment_artifact_id):
                return None

    async def handle_failed_treatment_gate(self, treatment_gate: EpisodeCritique, treatment_artifact_id: str) -> bool:
        await save_treatment_gate_artifact(
            self.db,
            self.run,
            treatment_gate,
            treatment_artifact_id=treatment_artifact_id,
            source_map_artifact_id=self.values["source_map_artifact_id"],
            source_artifact_ids=self.values["source_artifact_ids"],
        )
        repair_job_ids = await enqueue_treatment_research_repair_jobs(
            self.db,
            run=self.run,
            treatment_gate=treatment_gate,
        )
        if repair_job_ids:
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
            return True
        return False


def _repair_instructions_from_operation(operation: dict[str, Any] | None) -> list[str] | None:
    if not operation:
        return None
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    instructions = [str(item) for item in payload.get("repair_instructions") or [] if str(item).strip()]
    return instructions or None


__all__ = ["EditorialResearchStageRunner", "enqueue_treatment_research_repair_jobs"]
