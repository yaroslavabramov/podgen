"""Baseline A: complete draft -> independent informed review -> complete revision."""

from datetime import datetime, timezone

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.repository import Repository
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.materials import load_author_materials, load_own_plan_materials

from .compiler import COMPILER_VERSION, materials_hash
from .completion import completed_result
from .contracts import STAGES, RunFailure, WritingRunCommand, command_payload
from .journal import EvaluationJournal
from .manifest import create_manifest
from .planning import execute_planning
from .recovery import authorize_recovery
from .stage import execute_stage
from .workflow import output_model, stages_for


class WritingEvaluation:
    def __init__(
        self, repo, provider_factory, *, gateway_options=None, materials_loader=load_author_materials
    ):
        self.repo, self.provider_factory = repo, provider_factory
        self.materials_loader, self.gateway_options = materials_loader, gateway_options or {}
        self.generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")

    def _materials(self, command):
        loader = load_own_plan_materials if command.plan_mode == "own-plan" else self.materials_loader
        return loader(self.generation, command.project_id, command.author_ref, case_id=command.case_id)

    def run(
        self,
        command: WritingRunCommand,
        *,
        repair_review_anchors=False,
        retry_credit_refusal=None,
        repair_plan_targets=None,
    ):
        if command.actor_id != self.repo.actor_id:
            raise AccessDenied()
        journal = EvaluationJournal(self.repo, command.project_id)
        with journal.lock(command.run_id):
            existing = journal.run(command.run_id)
            if existing:
                if existing["input_fingerprint"] != digest(command_payload(command)):
                    raise Conflict("run_id_reused_with_different_input")
                manifest = journal.manifest(command.run_id)
                saved = [journal.invocation(command.run_id, stage) for stage in STAGES]
                if existing["status"] == "completed_unaccepted" or all(
                    item and item["status"] == "completed" for item in saved
                ):
                    authorize_recovery(
                        journal,
                        command,
                        credit_target=retry_credit_refusal,
                        plan_target=repair_plan_targets,
                        guard=lambda: None,
                        allow_create=False,
                    )
                    result = completed_result(self.generation, journal, command, manifest)
                    if existing["status"] != "completed_unaccepted":
                        journal.status(command.run_id, "completed_unaccepted")
                    return result
                materials = self._materials(command)
            else:
                if retry_credit_refusal is not None or repair_plan_targets is not None:
                    raise RunFailure("credit_refusal_retry_not_eligible", run_id=command.run_id)
                materials = self._materials(command)
                manifest = create_manifest(self.repo, self.generation, command, journal, materials)
            if manifest["compiler_version"] != COMPILER_VERSION:
                raise ContractError("compiler_version_changed")
            if materials_hash(materials.as_payload()) != manifest["materials_hash"]:
                raise Conflict("frozen_materials_changed")
            for stage in stages_for(command):
                if manifest["response_schemas"][stage] != output_model(stage).model_json_schema():
                    raise Conflict("role_output_contract_changed")
            gateway = ModelGateway(journal, self.provider_factory, **self.gateway_options)
            outputs = {}
            records = {}
            totals = {"input": 0, "output": 0}

            def guard():
                current = self._materials(command)
                if materials_hash(current.as_payload()) != manifest["materials_hash"]:
                    raise Conflict("frozen_materials_changed")
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_at"])
                ).total_seconds()
                if elapsed > command.limits.max_elapsed_seconds:
                    raise RunFailure("run_elapsed_budget_exceeded", run_id=command.run_id)

            authorize_recovery(
                journal,
                command,
                credit_target=retry_credit_refusal,
                plan_target=repair_plan_targets,
                guard=guard,
            )
            journal.status(command.run_id, "running")
            try:
                own_plan = None
                if command.plan_mode == "own-plan":
                    own_plan = execute_planning(
                        journal=journal,
                        gateway=gateway,
                        generation=self.generation,
                        command=command,
                        manifest=manifest,
                        materials=materials,
                        outputs=outputs,
                        records=records,
                        totals=totals,
                        guard=guard,
                    )
                for stage in STAGES:
                    guard()
                    result = execute_stage(
                        stage,
                        journal=journal,
                        gateway=gateway,
                        generation=self.generation,
                        command=command,
                        manifest=manifest,
                        materials=materials,
                        outputs=outputs,
                        records=records,
                        totals=totals,
                        guard=guard,
                        own_plan=own_plan,
                        repair_review_anchors=(
                            repair_review_anchors or command.review_anchor_policy == "bounded-selection-v1"
                        ),
                    )
                    outputs[stage] = result.output
                    records[stage] = result.ref.model_dump(mode="json")
                    totals["input"] += result.input_tokens
                    totals["output"] += result.output_tokens_or_reservation
                journal.status(command.run_id, "completed_unaccepted")
            except (RunFailure, ContractError) as error:
                journal.status(command.run_id, "needs_attention", error.code)
                raise
            return dict(
                run_id=str(command.run_id),
                case_id=command.case_id,
                status="completed_unaccepted",
                output_refs=records,
                semantic_acceptance=False,
                production_ready=False,
                token_usage_or_reservation=totals,
            )
