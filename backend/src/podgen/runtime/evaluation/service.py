"""Frozen writing runs with versioned review workflows and immutable script bindings."""

from datetime import datetime, timezone

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.repository import Repository
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.materials import load_author_materials, load_own_plan_materials
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.projection_versions import manifest_version

from .compiler import COMPILER_VERSION, materials_hash
from .completion import completed_result
from .contracts import STAGES, RunFailure, WritingRunCommand, command_payload
from .journal import EvaluationJournal
from .manifest import create_manifest
from .planning import execute_planning
from .reading_workflow import VERSION, independent
from .recovery import authorize_recovery
from .role_schema import response_schema
from .stage import execute_stage
from .workflow import stages_for


class WritingEvaluation:
    def __init__(
        self,
        repo,
        provider_factory,
        *,
        gateway_options=None,
        materials_loader=load_author_materials,
        legacy_offline_transport=None,
    ):
        self.repo, self.provider_factory = repo, provider_factory
        self.legacy_offline_transport = legacy_offline_transport
        self.materials_loader, self.gateway_options = materials_loader, gateway_options or {}
        self.generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")

    def _materials(self, command):
        if command.input_contract == "editorial-role-packets/1":
            from podgen.persistence.editorial_inputs import load_editorial_materials

            if (
                command.plan_mode == "fixed-plan"
                and command.fixed_plan_input_ref is None
                and command.initial_script_ref is None
            ):
                raise ContractError("fixed_editorial_input_required")
            return load_editorial_materials(
                self.generation,
                command.project_id,
                command.author_ref,
                case_id=command.case_id,
                fixed_ref=command.fixed_plan_input_ref,
            )
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
                historical = journal.manifest(command.run_id)["command"]
                if "input_contract" not in historical and "input_contract" not in command.model_fields_set:
                    command = command.model_copy(update={"input_contract": "legacy-author-materials/1"})
                if "review_mode" not in historical and "review_mode" not in command.model_fields_set:
                    command = command.model_copy(update={"review_mode": "informed-v1"})
                if existing["input_fingerprint"] != digest(command_payload(command)):
                    raise Conflict("run_id_reused_with_different_input")
                manifest = journal.manifest(command.run_id)
                if command.planning_parent_run_id is not None:
                    from .planning_continuation.source import verify

                    verify(journal, command, manifest, self._materials(command))
                if independent(command):
                    from .reading_outcome import outcome_result, read_outcome

                    outcome = read_outcome(journal, command.run_id)
                    if outcome is not None:
                        if (
                            materials_hash(self._materials(command).as_payload())
                            != manifest["materials_hash"]
                        ):
                            raise Conflict("frozen_materials_changed")
                        authorize_recovery(
                            journal,
                            command,
                            credit_target=retry_credit_refusal,
                            plan_target=repair_plan_targets,
                            guard=lambda: None,
                            allow_create=False,
                        )
                        result = outcome_result(outcome, command)
                        journal.status(command.run_id, result["status"], result["failure_code"])
                        return result
                saved = [journal.invocation(command.run_id, stage) for stage in STAGES]
                finalized = False
                if command.input_contract == "editorial-role-packets/1":
                    from .finalization import read_finalization

                    finalized = read_finalization(journal, command.run_id) is not None
                if (
                    finalized
                    or existing["status"] == "completed_unaccepted"
                    or all(item and item["status"] == "completed" for item in saved)
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
                if (
                    command.input_contract == "legacy-author-materials/1"
                    and self.legacy_offline_transport is None
                ):
                    raise ContractError("legacy_outbound_disabled_start_compact_run")
                materials = self._materials(command)
            else:
                if retry_credit_refusal is not None or repair_plan_targets is not None:
                    raise RunFailure("credit_refusal_retry_not_eligible", run_id=command.run_id)
                if (
                    command.input_contract == "legacy-author-materials/1"
                    and self.legacy_offline_transport is None
                ):
                    raise ContractError("legacy_outbound_disabled_start_compact_run")
                materials = self._materials(command)
                manifest = create_manifest(self.repo, self.generation, command, journal, materials)
            if command.input_contract == "editorial-role-packets/1":
                from podgen.writing.projections import SINGLE_INPUT_TOKEN_LIMIT, VISIBLE_BYTES_LIMIT

                if manifest.get("input_contract") != command.input_contract or manifest.get(
                    "role_input_budget"
                ) != dict(visible_bytes=VISIBLE_BYTES_LIMIT, input_tokens=SINGLE_INPUT_TOKEN_LIMIT):
                    raise ContractError("role_input_contract_changed")
            if independent(command) and manifest.get("review_workflow") != VERSION:
                raise ContractError("review_workflow_changed")
            if manifest["compiler_version"] != COMPILER_VERSION:
                raise ContractError("compiler_version_changed")
            if materials_hash(materials.as_payload()) != manifest["materials_hash"]:
                raise Conflict("frozen_materials_changed")
            references = (
                run_references(materials, manifest)
                if command.input_contract == "editorial-role-packets/1"
                else None
            )
            for stage in stages_for(command):
                if manifest["response_schemas"][stage] != bind_schema(
                    response_schema(
                        stage,
                        command.input_contract,
                        projection_version=manifest_version(manifest),
                        reader_schema_version=manifest.get("reader_review_schema_version"),
                    ),
                    references,
                ):
                    raise Conflict("role_output_contract_changed")
            factory = (
                (lambda _: self.legacy_offline_transport)
                if command.input_contract == "legacy-author-materials/1"
                else self.provider_factory
            )
            gateway = ModelGateway(journal, factory, **self.gateway_options)
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
                if independent(command):
                    from .reading_cycle import execute_reading_cycle
                    from .reading_outcome import outcome_result

                    outcome = execute_reading_cycle(
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
                        repair_review_anchors=repair_review_anchors
                        or command.review_anchor_policy == "bounded-selection-v1",
                    )
                    result = outcome_result(outcome, command)
                    journal.status(command.run_id, result["status"], result["failure_code"])
                    return result
                finalization = None
                for stage in STAGES:
                    guard()
                    if stage == "revise" and command.input_contract == "editorial-role-packets/1":
                        from .finalization import finalization_view, finalize_keep, review_action

                        if review_action(outputs["review"]) == "keep":
                            ref, record = finalize_keep(journal, command, outputs, records)
                            records["revise"] = ref.model_dump(mode="json")
                            finalization = finalization_view(record, ref)
                            break
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
            result = dict(
                run_id=str(command.run_id),
                case_id=command.case_id,
                status="completed_unaccepted",
                output_refs=records,
                semantic_acceptance=False,
                production_ready=False,
                token_usage_or_reservation=totals,
            )

            if finalization is not None:
                result["finalization"] = finalization
            return result
