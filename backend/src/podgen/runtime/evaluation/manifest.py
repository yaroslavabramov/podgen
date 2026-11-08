"""Pin every applicable instruction, schema, profile and complete source snapshot before inference."""

import json
import platform
from dataclasses import asdict
from importlib.metadata import version
from uuid import uuid5

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.repository import timestamp
from podgen.writing.author_reply import contract_pin, offered
from podgen.writing.craft_reference import load_reference
from podgen.writing.model_references import CATALOG_KEY, bind_schema, preview_references
from podgen.writing.planning_choice import KEY as PLANNING_KEY
from podgen.writing.planning_choice import for_input as planning_for_input
from podgen.writing.planning_duration import KEY as DURATION_KEY
from podgen.writing.planning_duration import for_input as duration_for_input
from podgen.writing.projection_versions import CURRENT
from podgen.writing.prompts import (
    load_anchor_repair_instructions,
    load_instruction_set,
    load_plan_target_repair_instructions,
)
from podgen.writing.reader_schema import V2 as LEGACY_READER_SCHEMA_VERSION
from podgen.writing.reader_schema import VERSION as READER_SCHEMA_VERSION
from podgen.writing.reading_views import reading_audience
from podgen.writing.revision.contracts import FACT_VERSION
from podgen.writing.revision.contracts import VERSION as REVISION_VERSION

from .compiler import COMPILER_VERSION, materials_hash
from .contracts import command_payload
from .profiles import load_profile
from .reading_workflow import VERSION, base_stage, independent
from .role_schema import response_schema
from .script_revision import schemas as revision_schemas
from .script_revision import supports as supports_revision
from .workflow import profile_for, prompt_stage, stages_for


def create_manifest(repo, generation, command, journal, materials):
    if command.planning_parent_run_id is not None:
        from .planning_continuation.manifest import create

        return create(command, journal, materials)
    planning_contract = (
        planning_for_input(command.input_contract, CURRENT) if command.plan_mode == "own-plan" else None
    )
    payload = materials.as_payload()
    revised = supports_revision(command)
    reader_version = READER_SCHEMA_VERSION if revised else LEGACY_READER_SCHEMA_VERSION
    reference = (
        load_reference(materials.output_language)
        if command.input_contract == "editorial-role-packets/1"
        and (command.initial_script_ref is None or command.max_script_revisions > 0)
        else None
    )
    references = preview_references(materials, CURRENT)
    producer = CodeProducer(
        kind="code",
        component="whole-episode-evaluation",
        build_id=COMPILER_VERSION,
        operation_id=command.run_id,
    )
    prompts = {}
    schemas = {}
    prompt_versions = {}
    instruction_set = (
        load_instruction_set(materials.format, include_planning=True)
        if command.plan_mode == "own-plan"
        else load_instruction_set(materials.format)
    )
    reading_prompts = {}
    if independent(command):
        from podgen.writing.prompts import load_reading_instructions

        reading_prompts = {
            name: load_reading_instructions(name) for name in ("reader-review", "fact-review", "dual-revise")
        }
    for stage in stages_for(command):
        profile_ref = profile_for(command, stage)
        load_profile(generation, command.project_id, profile_ref, stage)
        base = base_stage(stage)
        if independent(command) and base in ("reader-review", "fact-review", "revise"):
            prompt = reading_prompts["dual-revise" if base == "revise" else base]
        else:
            prompt = instruction_set[prompt_stage(stage)]
        instructions = prompt.body
        prompt_versions[stage] = prompt.version
        prompt = repo.create(
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
        schemas[stage] = bind_schema(
            response_schema(
                stage,
                command.input_contract,
                planning_output_contract=planning_contract,
                reader_schema_version=reader_version if independent(command) else None,
                fact_schema_version=FACT_VERSION if revised else None,
            ),
            references,
        )
    blob = journal.blob(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    )
    manifest = dict(
        contract="writing-evaluation-run-v1",
        input_contract=command.input_contract,
        output_language=materials.output_language,
        stages=list(stages_for(command)),
        plan_mode=command.plan_mode,
        runtime_versions={"python": platform.python_version(), "openai_sdk": version("openai")},
        compiler_version=COMPILER_VERSION,
        command=command_payload(command),
        author_ref=command_payload(command)["author_ref"],
        material_refs=[d.ref.model_dump(mode="json") for d in materials.documents],
        materials_blob=blob.model_dump(mode="json"),
        materials_hash=materials_hash(payload),
        prompt_refs=prompts,
        role_instructions_versions=prompt_versions,
        anchor_repair_instructions=asdict(load_anchor_repair_instructions()),
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
    if planning_contract is not None:
        manifest[PLANNING_KEY] = planning_contract
    planning_duration = (
        duration_for_input(command.input_contract, CURRENT) if command.plan_mode == "own-plan" else None
    )
    if planning_duration is not None:
        manifest[DURATION_KEY] = planning_duration
    if reference is not None:
        manifest["craft_reference"] = reference
    if references is not None:
        manifest[CATALOG_KEY] = references.as_payload()
    if independent(command) and command.initial_script_ref is not None:
        from .script_import import load_script

        script = load_script(repo, command.project_id, command.initial_script_ref, materials.speaker_labels)
        manifest["initial_script_hash"] = materials_hash(script)
    if independent(command):
        manifest["reading_audience"] = reading_audience(materials.packet.assignment)
        manifest["reader_review_schema_version"] = reader_version
        manifest["review_workflow"] = VERSION
        manifest["limitations"][1] = (
            "independent model readings; not evidence of human listening or production approval"
        )
    if revised:
        from podgen.writing.revision.context import KEY as REVISION_CONTEXT_KEY
        from podgen.writing.revision.context import VERSION as REVISION_CONTEXT_VERSION

        manifest[REVISION_CONTEXT_KEY] = REVISION_CONTEXT_VERSION
        manifest["fact_review_schema_version"] = FACT_VERSION
        manifest["script_revision_contract"] = REVISION_VERSION
        manifest["revision_response_schemas"] = revision_schemas()
    if command.input_contract == "editorial-role-packets/1":
        from podgen.writing.plan_selection import KEY as SELECTION_KEY
        from podgen.writing.plan_selection import for_projection
        from podgen.writing.projections import SINGLE_INPUT_TOKEN_LIMIT, VISIBLE_BYTES_LIMIT

        manifest["role_projection_version"] = CURRENT
        selection = for_projection(CURRENT)
        if selection is not None:
            manifest[SELECTION_KEY] = selection
        if offered("draft", CURRENT):
            manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
        manifest["role_input_budget"] = dict(
            visible_bytes=VISIBLE_BYTES_LIMIT, input_tokens=SINGLE_INPUT_TOKEN_LIMIT
        )
    if command.plan_mode == "own-plan":
        manifest["plan_review_target_catalog"] = "planning-output-pointers-v1"
        manifest["plan_target_repair_instructions"] = asdict(load_plan_target_repair_instructions())
    return journal.create_run(command, manifest)
