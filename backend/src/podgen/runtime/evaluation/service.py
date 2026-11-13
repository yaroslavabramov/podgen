"""Frozen writing runs with versioned review workflows and immutable script bindings."""

from datetime import datetime, timezone

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.repository import Repository
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.materials import load_author_materials, load_own_plan_materials
from podgen.writing.model_references import bind_schema, run_references
from podgen.writing.projection_versions import manifest_version
from podgen.writing.research_materials.schemas import adapt as research_schema

from . import author_material
from .compiler import COMPILER_VERSION, materials_hash
from .completion import completed_result
from .contracts import STAGES, RunFailure, WritingRunCommand, command_payload
from .journal import EvaluationJournal
from .manifest import create_manifest
from .planning import execute_planning
from .planning_checkpoint import PlanningCheckpoint
from .planning_checkpoint import result as plan_ready_result
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
                and command.editorial_parent_run_id is None
            ):
                raise ContractError("fixed_editorial_input_required")
            materials = load_editorial_materials(
                self.generation,
                command.project_id,
                command.author_ref,
                case_id=command.case_id,
                fixed_ref=command.fixed_plan_input_ref,
            )
            if getattr(materials.packet, "contract", None) == "research-editorial-input/1":
                from podgen.research.materials.resolver import ResearchMaterialResolver

                journal = EvaluationJournal(self.repo, command.project_id)
                materials.runtime_admissions[:] = ResearchMaterialResolver(
                    journal,
                    materials,
                    parent_run_ids=tuple(
                        parent
                        for parent in (command.planning_parent_run_id, command.editorial_parent_run_id)
                        if parent is not None
                    ),
                ).restore(run_id=command.run_id, guard=lambda: None)
            return materials
        loader = load_own_plan_materials if command.plan_mode == "own-plan" else self.materials_loader
        return loader(self.generation, command.project_id, command.author_ref, case_id=command.case_id)

    def run(
        self,
        command: WritingRunCommand,
        *,
        repair_review_anchors=False,
        retry_credit_refusal=None,
        repair_plan_targets=None,
        retry_input_budget=None,
        max_visible_bytes=None,
        max_input_tokens=None,
        retry_total_input_budget=None,
        max_total_input_tokens=None,
        stop_after_plan=False,
    ):
        if command.actor_id != self.repo.actor_id:
            raise AccessDenied()
        if stop_after_plan and command.plan_mode != "own-plan":
            raise ContractError("stop_after_plan_requires_own_plan")
        if stop_after_plan and command.editorial_parent_run_id is not None:
            raise ContractError("editorial_continuation_has_no_planning")
        journal = EvaluationJournal(self.repo, command.project_id)
        with journal.lock(command.run_id):
            existing = journal.run(command.run_id)
            if existing and journal.manifest(command.run_id).get("fact_review_experiment") is not None:
                raise ContractError("standalone_fact_experiment_requires_its_runner")
            from .run_configuration import production_command

            command = production_command(
                command, frozen_manifest=journal.manifest(command.run_id) if existing else None
            )
            checkpoint = PlanningCheckpoint(command.run_id)
            if existing:
                historical = journal.manifest(command.run_id)["command"]
                if "input_contract" not in historical and "input_contract" not in command.model_fields_set:
                    command = command.model_copy(update={"input_contract": "legacy-author-materials/1"})
                if "review_mode" not in historical and "review_mode" not in command.model_fields_set:
                    command = command.model_copy(update={"review_mode": "informed-v1"})
                if existing["input_fingerprint"] != digest(command_payload(command)):
                    raise Conflict("run_id_reused_with_different_input")
                manifest = journal.manifest(command.run_id)
                if command.input_contract == "editorial-role-packets/1":
                    from podgen.writing.research_materials.pinning import validate as validate_research

                    validate_research(self._materials(command), manifest)
                if command.plan_mode == "own-plan":
                    checkpoint = PlanningCheckpoint.load(journal, command.run_id)
                    if stop_after_plan and (
                        checkpoint.resumed or journal.invocation(command.run_id, "draft")
                    ):
                        raise Conflict("planning_checkpoint_already_passed")
                terminal = None
                if author_material.offered("draft", manifest.get("role_projection_version")):
                    from podgen.writing.author_reply import verify_pin

                    verify_pin(manifest, self._materials(command).speaker_labels)
                    terminal = author_material.saved_result(journal, command)
                if command.planning_parent_run_id is not None:
                    from .planning_continuation.source import verify

                    verify(journal, command, manifest, self._materials(command))
                if command.editorial_parent_run_id is not None:
                    from .editorial_continuation.source import verify

                    verify(journal, command, manifest, self._materials(command))
                if terminal is None and independent(command):
                    from .reading_outcome import outcome_result, read_outcome

                    outcome = read_outcome(journal, command.run_id)
                    if outcome is not None:
                        terminal = outcome_result(outcome, command)
                if terminal is not None:
                    if materials_hash(self._materials(command).as_payload()) != manifest["materials_hash"]:
                        raise Conflict("frozen_materials_changed")
                    authorize_recovery(
                        journal,
                        command,
                        credit_target=retry_credit_refusal,
                        plan_target=repair_plan_targets,
                        budget_target=retry_input_budget,
                        max_visible_bytes=max_visible_bytes,
                        max_input_tokens=max_input_tokens,
                        total_input_target=retry_total_input_budget,
                        max_total_input_tokens=max_total_input_tokens,
                        guard=lambda: None,
                        allow_create=False,
                    )
                    journal.status(command.run_id, terminal["status"], terminal["failure_code"])
                    return terminal
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
                        budget_target=retry_input_budget,
                        max_visible_bytes=max_visible_bytes,
                        max_input_tokens=max_input_tokens,
                        total_input_target=retry_total_input_budget,
                        max_total_input_tokens=max_total_input_tokens,
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
                if any(
                    value is not None
                    for value in (
                        retry_credit_refusal,
                        repair_plan_targets,
                        retry_input_budget,
                        max_visible_bytes,
                        max_input_tokens,
                        retry_total_input_budget,
                        max_total_input_tokens,
                    )
                ):
                    raise RunFailure("credit_refusal_retry_not_eligible", run_id=command.run_id)
                if (
                    command.input_contract == "legacy-author-materials/1"
                    and self.legacy_offline_transport is None
                ):
                    raise ContractError("legacy_outbound_disabled_start_compact_run")
                materials = self._materials(command)
                manifest = create_manifest(self.repo, self.generation, command, journal, materials)
            if command.input_contract == "editorial-role-packets/1":
                from .wire_budget import validate_manifest_budget

                if manifest.get("input_contract") != command.input_contract:
                    raise ContractError("role_input_contract_changed")
                validate_manifest_budget(manifest)
            from podgen.writing import (
                author_plan,
                ending_guidance,
                fact_context,
                prompt_cache,
                source_passages,
            )
            from podgen.writing.plan_selection import from_manifest as selection_from_manifest
            from podgen.writing.revision.context import from_manifest as revision_context_from_manifest

            author_plan.from_manifest(manifest)
            prompt_cache.from_manifest(manifest)
            fact_context.from_manifest(manifest)
            ending_version = ending_guidance.from_manifest(manifest)
            source_passages.from_manifest(manifest)
            selection_from_manifest(manifest)
            revision_context_from_manifest(manifest)
            from podgen.writing.planning_duration import from_manifest as duration_from_manifest

            duration_from_manifest(manifest)
            from podgen.writing.reading_audit.registry import from_manifest as audit_from_manifest

            audit_from_manifest(manifest)
            from podgen.writing.planning_choice import from_manifest as planning_from_manifest

            planning_contract = planning_from_manifest(manifest)
            from podgen.writing.plan_review_descriptions import schema_version_from_manifest

            plan_review_version = schema_version_from_manifest(manifest)
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
                    research_schema(
                        materials,
                        stage,
                        response_schema(
                            stage,
                            command.input_contract,
                            projection_version=manifest_version(manifest),
                            planning_output_contract=planning_contract,
                            plan_review_schema_version=plan_review_version,
                            planning_ending_guidance=ending_version,
                            reader_schema_version=manifest.get("reader_review_schema_version"),
                            fact_schema_version=manifest.get("fact_review_schema_version"),
                            reading_audit_consistency_contract=manifest.get(
                                "reading_audit_consistency_contract"
                            ),
                            revision_verification_contract=manifest.get("revision_verification_contract"),
                        ),
                    ),
                    references,
                ):
                    raise Conflict("role_output_contract_changed")
            from .script_revision import validate_manifest as validate_revision_manifest

            validate_revision_manifest(manifest)
            factory = (
                (lambda _: self.legacy_offline_transport)
                if command.input_contract == "legacy-author-materials/1"
                else self.provider_factory
            )
            from .source_tool_contract import from_manifest as source_contract

            corpus = None
            if source_contract(manifest) is not None:
                from podgen.retrieval import RetrievalUnavailable
                from podgen.writing.source_tools import SourceCorpus

                try:
                    retrieval = source_contract(manifest).get("retrieval")
                    index = None
                    if retrieval is not None:
                        from .source_search_index import PostgresSearchIndex

                        index_type = PostgresSearchIndex
                        if retrieval.get("version") == "hybrid-local/2":
                            from .streaming_search_index import StreamingSearchIndex

                            index_type = StreamingSearchIndex
                        index = index_type(
                            journal,
                            materials.shared_ref,
                            retrieval,
                            **(
                                {"run_id": command.run_id}
                                if retrieval.get("version") == "hybrid-local/2"
                                else {}
                            ),
                        )
                    corpus = SourceCorpus(materials, retrieval=retrieval, index=index)
                    if source_contract(manifest)["version"] == "frozen-corpus-tools/7":
                        from podgen.research.materials.resolver import ResearchMaterialResolver

                        corpus.material_resolver = ResearchMaterialResolver(journal, materials)
                except ImportError:
                    journal.status(command.run_id, "needs_attention", "source_search_dependencies_missing")
                    raise RunFailure("source_search_dependencies_missing", run_id=command.run_id) from None
                except RetrievalUnavailable as error:
                    code = str(error)
                    journal.status(command.run_id, "needs_attention", code)
                    raise RunFailure(code, run_id=command.run_id) from None
            gateway = ModelGateway(journal, factory, source_corpus=corpus, **self.gateway_options)
            outputs = {}
            records = {}
            totals = dict(
                manifest.get("editorial_continuation", {}).get("inherited_usage", {"input": 0, "output": 0})
            )

            def checkpoint_guard():
                now = datetime.now(timezone.utc)
                elapsed = (
                    now - datetime.fromisoformat(manifest["created_at"]) - checkpoint.wait_time(now)
                ).total_seconds()
                if elapsed > command.limits.max_elapsed_seconds:
                    raise RunFailure("run_elapsed_budget_exceeded", run_id=command.run_id)

            from .operation_guard import OperationGuard

            guard = OperationGuard(
                generation=self.generation,
                journal=journal,
                command=command,
                manifest=manifest,
                materials=materials,
                loader=self._materials,
                checkpoint=checkpoint_guard,
            )
            authorize_recovery(
                journal,
                command,
                credit_target=retry_credit_refusal,
                plan_target=repair_plan_targets,
                budget_target=retry_input_budget,
                max_visible_bytes=max_visible_bytes,
                max_input_tokens=max_input_tokens,
                total_input_target=retry_total_input_budget,
                max_total_input_tokens=max_total_input_tokens,
                guard=guard,
            )
            if existing and not any(
                target is not None
                for target in (
                    retry_credit_refusal,
                    repair_plan_targets,
                    retry_input_budget,
                    retry_total_input_budget,
                )
            ):
                from .profiles import load_profile
                from .technical_recovery import resume_current

                try:
                    resume_current(
                        journal,
                        command,
                        gateway=gateway,
                        guard=guard,
                        load_profile=lambda ref, stage: load_profile(
                            self.generation, command.project_id, ArtifactRef.model_validate(ref), stage
                        ),
                    )
                except (RunFailure, ContractError) as error:
                    journal.status(command.run_id, "needs_attention", error.code)
                    raise
            from .total_input_recovery import effective_ceiling

            gateway.total_input_ceiling = effective_ceiling(journal, command.run_id)
            if not stop_after_plan:
                checkpoint.mark(journal, "resumed", datetime.now(timezone.utc))
            journal.status(command.run_id, "running")
            try:
                own_plan = None
                if command.editorial_parent_run_id is not None:
                    from .editorial_continuation.source import own_plan as inherited_plan

                    own_plan = inherited_plan(journal, manifest)
                elif command.plan_mode == "own-plan":
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
                    if stop_after_plan:
                        checkpoint.mark(journal, "paused", datetime.now(timezone.utc))
                        journal.status(command.run_id, "plan_ready")
                        return plan_ready_result(command, own_plan, totals)
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
            except author_material.NeedsWritingMaterial as stop:
                journal.status(command.run_id, stop.result["status"], stop.result["failure_code"])
                return stop.result
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
