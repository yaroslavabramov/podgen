"""Evaluate one bound task with the normal counted model gateway and read receipts."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid5

from scopes import FOCUS, METHOD, PARTITION, SYNTHESIS, Partition, Synthesis, validate_dispositions

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import CodeProducer
from podgen.domain.errors import Conflict
from podgen.persistence.repository import timestamp
from podgen.runtime.evaluation import source_tool_contract
from podgen.runtime.evaluation.compiler import response_request
from podgen.runtime.evaluation.contracts import command_payload
from podgen.runtime.evaluation.operation_guard import OperationGuard
from podgen.runtime.evaluation.reading_audit.lifecycle import finalize
from podgen.runtime.evaluation.reading_audit.source import AuditContext
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from podgen.runtime.evaluation.validation import load_unique_json
from podgen.runtime.models.gateway import ModelGateway
from podgen.writing.behavior import resolve_behavior
from podgen.writing.projections import indexed_draft
from podgen.writing.reading_audit import consistency, registry
from podgen.writing.reading_audit.input import attach, initial_context
from podgen.writing.reading_audit.schemas import AuditedFact
from podgen.writing.research_materials import fact_source_instructions, fact_source_mode
from podgen.writing.research_materials.pinning import instructions as material_instructions
from podgen.writing.research_materials.schemas import adapt as material_schema
from podgen.writing.source_tools import SourceCorpus


class Trial:
    def __init__(self, case, repo, generation, journal, factory, metrics):
        self.case, self.repo, self.generation, self.journal = case, repo, generation, journal
        self.factory, self.metrics = factory, metrics
        self.contract = deepcopy(case.manifest["source_tool_contract"])
        if self.contract["version"] != source_tool_contract.DURABLE_RESEARCH_VERSION:
            raise Conflict("fact_scope_requires_frozen_source_snapshot_v7")
        self.contract.update(
            definitions=fact_source_mode.tools(self.contract["definitions"]),
            **{fact_source_mode.KEY: fact_source_mode.VERSION},
        )
        role = resolve_behavior("production/1").instructions("fact-review", case.materials.format)
        self.instructions = fact_source_instructions.adapt(
            material_instructions(case.materials, "fact-review", role.body)
        )
        self.schema = fact_source_mode.schema(
            material_schema(
                case.materials,
                "fact-review",
                registry.schema("fact-review", consistency_contract=consistency.CURRENT),
            )
        )
        self.schema_version = consistency.CURRENT
        self.totals = dict(input=0, output=0)

    def request(self, run_id, kind, start, stop, proofs, sections):
        case = self.case
        script = dict(utterances=case.script["utterances"][start:stop])
        payload = dict(output_language=case.materials.output_language, script=indexed_draft(script))
        view = initial_context(fact=True)
        if kind == "partition":
            return response_request(
                run_id, "fact-review", case.profile, payload, PARTITION, Partition.model_json_schema()
            ), None
        schema = deepcopy(self.schema)
        instructions = self.instructions + "\n\n" + METHOD
        if kind == "fragment":
            payload.update(
                episode_context=indexed_draft(case.script), assignment_starts_at_global_index=start
            )
            instructions += "\n" + FOCUS
        if kind == "synthesis":
            payload["section_reports"] = sections
            instructions += "\n" + SYNTHESIS
            outcome = Synthesis.model_json_schema()
            schema["$defs"]["Disposition"] = outcome["$defs"]["Disposition"]
            schema["properties"]["fragment_dispositions"] = outcome["properties"]["fragment_dispositions"]
            schema["required"].append("fragment_dispositions")
        view["previous_evidence"] = [
            dict(evidence_id=alias, kind=value["kind"], content=value["content"])
            for alias, value in proofs.items()
        ]
        payload.update(
            source_catalog=[dict(source_id=key, title=doc["title"]) for key, doc in case.inventory.items()]
        )
        payload["other_readable_passages"] = [
            {k: content[k] for k in ("source_id", "start", "end")}
            for row in case.evidence
            if (content := row["receipt"]["content"]).get("source_id")
            and row["receipt"] not in proofs.values()
        ]
        attach(payload, view, fact=True)
        body = response_request(
            run_id,
            "fact-review",
            case.profile,
            payload,
            instructions,
            schema,
            prompt_cache_layout=case.manifest["prompt_cache_layout"],
        )
        source_tool_contract.bind(body, "fact-review", case.profile, self.contract)
        return body, AuditContext(
            view,
            dict(script_hash=digest(script), frozen_sources=digest(case.inventory)),
            proofs,
            case.snapshots,
            self.schema_version,
        )

    def admit(self, command, body, binding):
        run_id, journal, case = command.run_id, self.journal, self.case
        if journal.run(run_id):
            return journal.manifest(run_id)
        manifest = dict(
            deepcopy(case.manifest),
            command=command_payload(command),
            created_at=timestamp(),
            stages=["fact-review"],
            fact_scope_comparison=binding,
            source_tool_contract=self.contract,
            **{fact_source_mode.KEY: fact_source_mode.VERSION, consistency.KEY: self.schema_version},
        )
        manifest.pop("planning_continuation", None)
        prompt = self.repo.create(
            command.project_id,
            "PromptDefinition",
            dict(
                role="fact-review",
                template=body["instructions"],
                placeholders=[],
                examples=[],
                compiler_version=manifest["compiler_version"],
            ),
            producer=CodeProducer(
                kind="code",
                component="fact-scope-comparison",
                build_id=binding["contract"],
                operation_id=run_id,
            ),
            operation_id=uuid5(run_id, "prompt:fact-review"),
        )
        manifest.update(
            prompt_refs={"fact-review": prompt.ref.model_dump(mode="json")},
            response_schemas={"fact-review": body["text"]["format"]["schema"]},
            role_instructions_versions={"fact-review": digest(body["instructions"])},
        )
        return journal.create_run(command, manifest)

    def gateway(self, kind, run_id):
        journal, case = self.journal, self.case
        if kind == "partition":
            return ModelGateway(journal, self.factory)
        retrieval = self.contract["retrieval"]
        index = StreamingSearchIndex(journal, case.materials.shared_ref, retrieval, run_id=run_id)
        index.access.run_id = run_id
        corpus = SourceCorpus(
            case.materials, retrieval=retrieval, index=index, fact_input_mode=fact_source_mode.VERSION
        )
        corpus.bind_execution(journal.execution, run_id)
        self.metrics.instrument_search(corpus)
        return ModelGateway(journal, self.factory, source_corpus=corpus)

    def execute(self, root_id, label, kind, start, stop, proofs, sections, issues):
        case, journal = self.case, self.journal
        run_id = uuid5(root_id, label)
        command = case.command.model_copy(
            update=dict(run_id=run_id, planning_parent_run_id=None, planning_continuation_limit=None)
        )
        body, audit = self.request(run_id, kind, start, stop, proofs, sections)
        binding = dict(
            contract="fact-scope-comparison/2",
            **case.binding,
            condition=label,
            start=start,
            stop=stop,
            request_hash=digest(body),
            proof_hash=digest(proofs),
        )
        with journal.lock(run_id):
            manifest = self.admit(command, body, binding)
            if (
                manifest.get("fact_scope_comparison") != binding
                or manifest["source_tool_contract"] != self.contract
            ):
                raise Conflict("fact_scope_changed_since_run")
            deadline = (
                datetime.fromisoformat(manifest["created_at"]).timestamp()
                + command.limits.max_elapsed_seconds
            )

            def checkpoint():
                if datetime.now(timezone.utc).timestamp() > deadline:
                    raise Conflict("fact_scope_run_elapsed")

            guard = OperationGuard(
                generation=self.generation,
                journal=journal,
                command=command,
                manifest=manifest,
                materials=case.materials,
                loader=None,
                checkpoint=checkpoint,
            )
            guard()
            invocation = journal.prepare(
                run_id,
                "fact-review",
                body,
                dict(
                    experiment=binding,
                    script_ref=case.script_ref,
                    role_input_contract=command.input_contract,
                    role_projection_version=manifest["role_projection_version"],
                    prompt_ref=manifest["prompt_refs"]["fact-review"],
                ),
            )
            if digest(journal.request(invocation)) != digest(body):
                raise Conflict("fact_scope_request_changed")
            if invocation["status"] == "completed":
                record = journal.output(invocation)
                if record.get("binding") != binding or record.get("script_ref") != case.script_ref:
                    raise Conflict("fact_scope_saved_result_changed")
                return self.account(record, invocation)
            if invocation["status"] in ("failed", "validation_failed", "outcome_unknown"):
                raise Conflict("fact_scope_stage_requires_reconciliation", label)
            gateway = self.gateway(kind, run_id)
            journal.status(run_id, "running")
            try:
                invoke = gateway.invoke_once if kind == "partition" else gateway.invoke
                raw = invoke(
                    invocation,
                    case.profile,
                    limits=command.limits,
                    totals=self.totals,
                    guard=guard,
                )
                value = load_unique_json(raw)
                if kind == "partition":
                    partition = Partition.model_validate(value)
                    list(partition.ranges(stop))
                    record = dict(output=value)
                else:
                    if kind == "synthesis":
                        checked = Synthesis.model_validate(value).model_dump(mode="json")
                        validate_dispositions(checked, issues)
                        value.pop("fragment_dispositions")
                    result, proof = finalize(
                        journal,
                        invocation,
                        AuditedFact.model_validate(value),
                        audit,
                        case.materials,
                        dict(utterances=case.script["utterances"][start:stop]),
                    )
                    record = dict(output=result, reading_audit=proof)
                    if kind == "fragment":
                        record["finding_passages"] = [
                            [
                                dict(
                                    from_review="previous" if alias.startswith("H") else "current",
                                    evidence_id=alias,
                                )
                                for alias in row["source_read_ids"]
                            ]
                            for row in value["findings"]
                        ]
                    if kind == "synthesis":
                        record["fragment_dispositions"] = checked["fragment_dispositions"]
                record.update(binding=binding, script_ref=case.script_ref)
                saved = journal.record(record, record_id=uuid5(invocation["invocation_id"], "output"))
                journal.transition(invocation, "completed", output_ref=saved)
                journal.status(run_id, "completed_unaccepted")
                return self.account(record, journal.invocation(run_id, "fact-review"))
            except Exception as exc:
                journal.status(run_id, "needs_attention", getattr(exc, "code", type(exc).__name__))
                raise

    def account(self, record, invocation):
        for key in self.totals:
            value = invocation[key + "_tokens"]
            if value is None:
                raise Conflict("fact_scope_missing_completed_usage")
            self.totals[key] += value
        return record
