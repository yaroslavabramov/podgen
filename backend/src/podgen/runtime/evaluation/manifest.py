"""Pin every applicable instruction, schema, profile and complete source snapshot before inference."""

import json
import platform
from dataclasses import asdict
from importlib.metadata import version
from uuid import uuid5

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.repository import timestamp
from podgen.writing import author_plan, ending_guidance, fact_context, prompt_cache, source_passages
from podgen.writing.author_reply import contract_pin, offered
from podgen.writing.behavior import resolve_behavior
from podgen.writing.model_references import CATALOG_KEY, bind_schema, preview_references
from podgen.writing.plan_review_descriptions import PRESERVE_KEY
from podgen.writing.planning_choice import KEY as PLANNING_KEY
from podgen.writing.planning_duration import KEY as DURATION_KEY
from podgen.writing.reader_schema import V2 as LEGACY_READER_SCHEMA_VERSION
from podgen.writing.reading_audit.consistency import KEY as CONSISTENCY_KEY
from podgen.writing.reading_audit.consistency import VERSION as CONSISTENCY_VERSION
from podgen.writing.reading_audit.memory_projection import KEY as MEMORY_KEY
from podgen.writing.reading_audit.memory_projection import VERSION as MEMORY_VERSION
from podgen.writing.research_materials.schemas import adapt as research_schema
from podgen.writing.revision.contracts import VERSION as REVISION_VERSION

from . import source_tool_contract
from .compiler import COMPILER_VERSION, materials_hash
from .contracts import command_payload
from .profiles import load_profile
from .reading_workflow import VERSION, base_stage, independent
from .role_schema import response_schema
from .script_revision import schemas as revision_schemas
from .script_revision import supports as supports_revision
from .wire_budget import input_budget_for_profile
from .workflow import profile_for, prompt_stage, stages_for


def create_manifest(repo, generation, command, journal, materials):
    if command.editorial_parent_run_id is not None:
        from .editorial_continuation.manifest import create

        return create(command, journal, materials)
    if command.planning_parent_run_id is not None:
        from .planning_continuation.manifest import create

        return create(command, journal, materials)
    behavior = resolve_behavior(command.behavior_profile, command.editorial_pass)
    editorial = command.input_contract == "editorial-role-packets/1"
    planning_contract = behavior.planning_contract if editorial and command.plan_mode == "own-plan" else None
    payload = materials.as_payload()
    cache_layout = prompt_cache.for_materials(behavior.prompt_cache_layout, materials) if editorial else None
    revised = supports_revision(command)
    reader_version = behavior.reader_schema if revised else LEGACY_READER_SCHEMA_VERSION
    reference = (
        behavior.craft(materials.output_language)
        if command.input_contract == "editorial-role-packets/1"
        and (command.initial_script_ref is None or command.max_script_revisions > 0)
        else None
    )
    references = preview_references(materials, behavior.projection)
    tools = source_tool_contract.create(behavior, materials) if editorial else None
    producer = CodeProducer(
        kind="code",
        component="whole-episode-evaluation",
        build_id=COMPILER_VERSION,
        operation_id=command.run_id,
    )
    from podgen.runtime.models.provider_contracts import KEY as PROVIDER_KEY
    from podgen.runtime.models.provider_contracts import contract as provider_contract

    provider_contracts = {}
    prompts = {}
    schemas = {}
    prompt_versions = {}
    for stage in stages_for(command):
        profile = load_profile(generation, command.project_id, profile_for(command, stage), stage)
        behavior.validate_model(stage, profile)
        provider_contracts[stage] = provider_contract(profile)
        base = base_stage(stage)
        if independent(command) and base in ("reader-review", "fact-review", "revise"):
            role = "dual-revise" if base == "revise" else base
            if base == "reader-review" and stage != base and behavior.verification:
                role = "revision-verification"
            if base == "fact-review" and stage != base and behavior.reading_audit:
                role = "fact-verification"
        else:
            role = prompt_stage(stage)
        prompt = behavior.instructions(role, materials.format)
        from podgen.writing.research_materials.pinning import instructions as adapt_instructions

        instructions = adapt_instructions(materials, role, prompt.body)
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
            research_schema(
                materials,
                stage,
                response_schema(
                    stage,
                    command.input_contract,
                    projection_version=behavior.projection,
                    plan_review_schema_version=behavior.plan_review_schema if editorial else None,
                    planning_ending_guidance=behavior.planning_ending_guidance if editorial else None,
                    planning_output_contract=planning_contract,
                    **(
                        {"revision_verification_contract": behavior.verification}
                        if behavior.verification
                        else {}
                    ),
                    reader_schema_version=reader_version if independent(command) else None,
                    fact_schema_version=behavior.fact_schema if revised else None,
                    reading_audit_consistency_contract=CONSISTENCY_VERSION
                    if behavior.reading_audit
                    else None,
                ),
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
        anchor_repair_instructions=asdict(behavior.instructions("anchor-repair", materials.format)),
        behavior_profile=behavior.name,
        editorial_pass=behavior.editorial_pass,
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
    from podgen.writing.research_materials.pinning import KEY as RESEARCH_KEY
    from podgen.writing.research_materials.pinning import pin as research_pin

    research = research_pin(materials)
    if research is not None:
        manifest[RESEARCH_KEY] = research
    manifest[PROVIDER_KEY] = provider_contracts
    if editorial and behavior.plan_review_schema is not None:
        manifest[PRESERVE_KEY] = behavior.plan_review_schema
    if tools is not None:
        manifest[source_tool_contract.KEY] = tools
    if editorial and behavior.planning_ending_guidance is not None:
        manifest[ending_guidance.KEY] = behavior.planning_ending_guidance
    if behavior.verification:
        manifest["revision_verification_contract"] = behavior.verification
    if planning_contract is not None:
        manifest[PLANNING_KEY] = planning_contract
    planning_duration = behavior.planning_duration if editorial and command.plan_mode == "own-plan" else None
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
        if behavior.reading_audience_from_brief:
            manifest["reading_audience"] = behavior.audience(materials.packet.assignment)
        manifest["reader_review_schema_version"] = reader_version
        manifest["review_workflow"] = VERSION
        if behavior.reading_audit:
            manifest["reading_audit_contract"] = behavior.reading_audit
            manifest[CONSISTENCY_KEY] = CONSISTENCY_VERSION
            manifest[MEMORY_KEY] = MEMORY_VERSION
        manifest["limitations"][1] = (
            "independent model readings; not evidence of human listening or production approval"
        )
    if revised:
        from podgen.writing.revision.context import KEY as REVISION_CONTEXT_KEY

        if behavior.revision_context:
            manifest[REVISION_CONTEXT_KEY] = behavior.revision_context
        manifest["fact_review_schema_version"] = behavior.fact_schema
        if editorial and behavior.fact_context is not None:
            manifest[fact_context.KEY] = behavior.fact_context
        manifest["script_revision_contract"] = REVISION_VERSION
        manifest["revision_response_schemas"] = revision_schemas(prompt_cache_layout=cache_layout)
    if command.input_contract == "editorial-role-packets/1":
        from podgen.writing.plan_selection import KEY as SELECTION_KEY

        manifest["role_projection_version"] = behavior.projection
        if cache_layout:
            manifest[prompt_cache.KEY] = cache_layout
        if behavior.author_source_excerpts:
            manifest[source_passages.KEY] = behavior.author_source_excerpts
        if behavior.author_plan_handoff:
            manifest[author_plan.KEY] = behavior.author_plan_handoff
        selection = behavior.selection
        if selection is not None:
            manifest[SELECTION_KEY] = selection
        if offered("draft", behavior.projection):
            manifest["writer_response_contract"] = contract_pin(manifest, materials.speaker_labels)
        manifest["role_input_budget"] = input_budget_for_profile(behavior.name)
    if command.plan_mode == "own-plan":
        manifest["plan_review_target_catalog"] = "planning-output-pointers-v1"
        manifest["plan_target_repair_instructions"] = asdict(
            behavior.instructions("plan-target-repair", materials.format)
        )
    return journal.create_run(command, manifest)
