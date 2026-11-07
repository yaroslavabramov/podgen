"""Pin every applicable instruction, schema, profile and complete source snapshot before inference."""

import json
import platform
from dataclasses import asdict
from importlib.metadata import version
from uuid import uuid5

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.repository import timestamp
from podgen.writing.craft_reference import load_reference
from podgen.writing.model_references import CATALOG_KEY, bind_schema, preview_references
from podgen.writing.projection_versions import CURRENT
from podgen.writing.prompts import (
    load_anchor_repair_instructions,
    load_instruction_set,
    load_plan_target_repair_instructions,
)
from podgen.writing.reader_schema import VERSION as READER_SCHEMA_VERSION

from .compiler import COMPILER_VERSION, materials_hash
from .contracts import command_payload
from .profiles import load_profile
from .reading_workflow import VERSION, base_stage, independent
from .role_schema import response_schema
from .workflow import profile_for, prompt_stage, stages_for


def create_manifest(repo, generation, command, journal, materials):
    if command.planning_parent_run_id is not None:
        from .planning_continuation.manifest import create

        return create(command, journal, materials)
    payload = materials.as_payload()
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
                reader_schema_version=READER_SCHEMA_VERSION if independent(command) else None,
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
    if reference is not None:
        manifest["craft_reference"] = reference
    if references is not None:
        manifest[CATALOG_KEY] = references.as_payload()
    if independent(command) and command.initial_script_ref is not None:
        from .script_import import load_script

        script = load_script(repo, command.project_id, command.initial_script_ref, materials.speaker_labels)
        manifest["initial_script_hash"] = materials_hash(script)
    if independent(command):
        manifest["reader_review_schema_version"] = READER_SCHEMA_VERSION
        manifest["review_workflow"] = VERSION
        manifest["limitations"][1] = (
            "independent model readings; not evidence of human listening or production approval"
        )
    if command.input_contract == "editorial-role-packets/1":
        from podgen.writing.projections import SINGLE_INPUT_TOKEN_LIMIT, VISIBLE_BYTES_LIMIT

        manifest["role_projection_version"] = CURRENT
        manifest["role_input_budget"] = dict(
            visible_bytes=VISIBLE_BYTES_LIMIT, input_tokens=SINGLE_INPUT_TOKEN_LIMIT
        )
    if command.plan_mode == "own-plan":
        manifest["plan_review_target_catalog"] = "planning-output-pointers-v1"
        manifest["plan_target_repair_instructions"] = asdict(load_plan_target_repair_instructions())
    return journal.create_run(command, manifest)
