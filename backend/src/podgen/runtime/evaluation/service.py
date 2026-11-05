"""Baseline A: complete draft -> independent informed review -> complete revision."""

import json
import platform
from datetime import datetime, timezone
from importlib.metadata import version
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import CodeProducer
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.persistence.repository import Repository, timestamp
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.materials import load_author_materials
from podgen.writing.prompts import load_instruction_set
from podgen.writing.roles import EditorialFeedback, WriteOutput

from .compiler import COMPILER_VERSION, materials_hash
from .completion import completed_result
from .contracts import STAGES, RunFailure, WritingRunCommand, command_payload
from .journal import EvaluationJournal
from .profiles import load_profile
from .stage import execute_stage


class WritingEvaluation:
    def __init__(
        self, repo, provider_factory, *, gateway_options=None, materials_loader=load_author_materials
    ):
        self.repo, self.provider_factory = repo, provider_factory
        self.materials_loader, self.gateway_options = materials_loader, gateway_options or {}
        self.generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")

    def _materials(self, command):
        return self.materials_loader(
            self.generation, command.project_id, command.author_ref, case_id=command.case_id
        )

    def _create_manifest(self, command, journal, materials):
        payload = materials.as_payload()
        producer = CodeProducer(
            kind="code",
            component="whole-episode-evaluation",
            build_id=COMPILER_VERSION,
            operation_id=command.run_id,
        )
        prompts = {}
        schemas = {}
        prompt_versions = {}
        instruction_set = load_instruction_set(materials.format)
        for stage in STAGES:
            profile_ref = command.reviewer_profile_ref if stage == "review" else command.writer_profile_ref
            load_profile(self.generation, command.project_id, profile_ref, stage)
            instructions = instruction_set[stage].body
            prompt_versions[stage] = instruction_set[stage].version
            prompt = self.repo.create(
                command.project_id,
                "PromptDefinition",
                dict(
                    role=stage,
                    template=instructions,
                    placeholders=[],
                    examples=[],
                    compiler_version=COMPILER_VERSION,
                ),
                producer=producer,
                operation_id=uuid5(command.run_id, "prompt:" + stage),
            )
            prompts[stage] = prompt.ref.model_dump(mode="json")
            schemas[stage] = (EditorialFeedback if stage == "review" else WriteOutput).model_json_schema()
        blob = journal.blob(
            json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        )
        manifest = dict(
            contract="writing-evaluation-run-v1",
            output_language=materials.output_language,
            stages=list(STAGES),
            runtime_versions={"python": platform.python_version(), "openai_sdk": version("openai")},
            compiler_version=COMPILER_VERSION,
            command=command_payload(command),
            author_ref=command.author_ref.model_dump(mode="json"),
            material_refs=[d.ref.model_dump(mode="json") for d in materials.documents],
            materials_blob=blob.model_dump(mode="json"),
            materials_hash=materials_hash(payload),
            prompt_refs=prompts,
            role_instructions_versions=prompt_versions,
            response_schemas=schemas,
            created_at=timestamp(),
            purpose="evaluation",
            semantic_acceptance=False,
            production_ready=False,
            strategy="A/whole-episode",
            source_access="frozen-text-v1",
            limitations=[
                "unaccepted staged inputs",
                "informed model review; not blind EXP-04 or independent fact-check",
                "technical script versions; canonical EpisodeSpec/StoryPlan/ScriptRevision mapping remains open",
            ],
        )
        return journal.create_run(command, manifest)

    def run(self, command: WritingRunCommand, *, repair_review_anchors=False):
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
                    result = completed_result(self.generation, journal, command, manifest)
                    if existing["status"] != "completed_unaccepted":
                        journal.status(command.run_id, "completed_unaccepted")
                    return result
                materials = self._materials(command)
            else:
                materials = self._materials(command)
                manifest = self._create_manifest(command, journal, materials)
            if manifest["compiler_version"] != COMPILER_VERSION:
                raise ContractError("compiler_version_changed")
            if materials_hash(materials.as_payload()) != manifest["materials_hash"]:
                raise Conflict("frozen_materials_changed")
            for stage in STAGES:
                if (
                    manifest["response_schemas"][stage]
                    != (EditorialFeedback if stage == "review" else WriteOutput).model_json_schema()
                ):
                    raise Conflict("role_output_contract_changed")
            gateway = ModelGateway(journal, self.provider_factory, **self.gateway_options)
            outputs = {}
            records = {}
            totals = {"input": 0, "output": 0}
            journal.status(command.run_id, "running")

            def guard():
                current = self._materials(command)
                if materials_hash(current.as_payload()) != manifest["materials_hash"]:
                    raise Conflict("frozen_materials_changed")
                elapsed = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(manifest["created_at"])
                ).total_seconds()
                if elapsed > command.limits.max_elapsed_seconds:
                    raise RunFailure("run_elapsed_budget_exceeded", run_id=command.run_id)

            try:
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
