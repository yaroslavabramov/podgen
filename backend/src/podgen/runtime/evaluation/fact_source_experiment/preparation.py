"""Derive one pinned role request from an existing run without importing its decisions."""

from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import CodeProducer, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.repository import timestamp
from podgen.writing.reading_audit.input import initial_context
from podgen.writing.research_materials.fact_source_mode import KEY, VERSION, request, schema, tools
from podgen.writing.research_materials.source_instruction_variant import from_manifest as source_variant
from podgen.writing.research_materials.source_instruction_variant import resolve as source_instructions
from podgen.writing.research_materials.views import source_catalog
from podgen.writing.scene_plan import is_scene_plan
from podgen.writing.source_tools import SourceCorpus

from ..contracts import WritingRunCommand, command_payload
from ..preview import preview_request
from ..preview_snapshot import PreviewSnapshot
from ..profiles import load_profile
from ..service import WritingEvaluation


@dataclass
class PreparedFact:
    command: object
    manifest: dict
    materials: object
    script: dict
    script_ref: dict
    approved_plan: dict | None
    profile: object
    body: dict
    source_body: dict
    sources: list


def prepare(repo, generation, journal, source_run_id, run_id):
    if run_id == source_run_id:
        raise Conflict("fact_experiment_requires_new_run")
    source = journal.manifest(source_run_id)
    old_command = WritingRunCommand.model_validate(source["command"])
    if old_command.actor_id != repo.actor_id or old_command.project_id != journal.project:
        raise Conflict("fact_experiment_scope_mismatch")
    invocation = journal.invocation(source_run_id, "fact-review")
    if invocation is None:
        raise Conflict("fact_experiment_source_request_missing")
    # Read the original request, never the prior factual output/decisions.
    source_body = journal.request(invocation)
    record = journal.read(RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"]))
    script_ref = record["context"]["script_ref"]
    script_record = journal.read(RuntimeRef.model_validate(script_ref))
    script = script_record["output"]
    loader = WritingEvaluation(repo, lambda _: None)
    materials = loader._materials(old_command)
    # This comparison starts with the exact original frozen documents, not later
    # run-local additions or another role's supplemental investigations.
    materials = replace(materials, runtime_admissions=[], runtime_annex={})
    original = SourceCorpus(materials)
    restricted = SourceCorpus(materials, fact_input_mode=VERSION)

    def signatures(corpus):
        return {key: (doc.title, doc.digest, len(doc.text)) for key, doc in corpus.documents.items()}

    if signatures(original) != signatures(restricted):
        raise Conflict("fact_experiment_source_documents_changed")
    specs = {doc.key: doc for doc in materials.packet.documents}
    by_id = {row.source_id: row for row in materials.packet.sources}
    sources = source_catalog(materials, include_reported_url=True)
    for row in sources:
        source_row = by_id[row["source_id"]]
        row["sha256"] = specs[source_row.document].sha256 if source_row.document else None
        document = restricted.documents.get(row["source_id"])
        row["length"] = len(document.text) if document is not None else None
    command = old_command.model_copy(update={"run_id": run_id})
    manifest = deepcopy(source)
    manifest.update(
        command=command_payload(command), stages=["fact-review"], created_at=timestamp(), **{KEY: VERSION}
    )
    contract = deepcopy(manifest["source_tool_contract"])
    contract.update(definitions=tools(contract["definitions"]), **{KEY: VERSION})
    manifest["source_tool_contract"] = contract
    manifest["response_schemas"]["fact-review"] = schema(manifest["response_schemas"]["fact-review"])
    pinned_variant = source_variant(source)
    instructions = source_instructions(pinned_variant, source_body["instructions"])
    from podgen.writing.prompts.composition import identity

    manifest["role_instructions_versions"]["fact-review"] = identity("fact-review", instructions)
    profile = load_profile(generation, journal.project, old_command.reviewer_profile_ref, "fact-review")
    plan_ref = script_record.get("accepted_plan_ref")
    plan = journal.read(RuntimeRef.model_validate(plan_ref))["output"] if plan_ref else None
    if not is_scene_plan(plan):
        plan = None
    manifest["fact_review_experiment"] = dict(
        contract="fact-review-report-access-experiment/1",
        source_run_id=str(source_run_id),
        source_request_hash=digest(source_body),
        script_ref=script_ref,
        script_hash=digest(script),
        source_inventory_hash=digest(sources),
    )
    snapshot = PreviewSnapshot(
        manifest, instructions, script=script, reading_audit_context=initial_context(fact=True)
    )
    body = preview_request(
        run_id, "fact-review", profile, materials, snapshot=snapshot, draft=script, approved_plan=plan
    )
    expected = request(source_body, VERSION, source_instruction_variant=pinned_variant)
    expected["metadata"].update(
        podgen_run_id=str(run_id), podgen_invocation_id=str(uuid5(run_id, "invocation:fact-review"))
    )
    if body != expected:
        raise Conflict("fact_experiment_uncontrolled_request_difference")
    return PreparedFact(
        command, manifest, materials, script, script_ref, plan, profile, body, source_body, sources
    )


def admit(repo, journal, prepared):
    """Idempotently pin already prepared data; never reload the source corpus."""
    manifest = deepcopy(prepared.manifest)
    run_id = prepared.command.run_id
    existing = journal.run(run_id)
    if existing is not None:
        saved = journal.manifest(run_id)
        if (
            saved.get("fact_review_experiment") != manifest["fact_review_experiment"]
            or saved.get(KEY) != VERSION
            or saved["source_tool_contract"] != manifest["source_tool_contract"]
        ):
            raise Conflict("fact_experiment_binding_changed")
        prepared.manifest = saved
        return prepared
    producer = CodeProducer(
        kind="code", component="fact-review-input-mode", build_id=VERSION, operation_id=run_id
    )
    prompt = repo.create(
        journal.project,
        "PromptDefinition",
        dict(
            role="fact-review",
            template=prepared.body["instructions"],
            placeholders=[],
            examples=[],
            compiler_version=manifest["compiler_version"],
        ),
        producer=producer,
        operation_id=uuid5(run_id, "source-only-fact-prompt"),
    )
    manifest["prompt_refs"]["fact-review"] = prompt.ref.model_dump(mode="json")
    prepared.manifest = journal.create_run(prepared.command, manifest)
    return prepared
