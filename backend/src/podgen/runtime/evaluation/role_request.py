"""One compiler boundary for all modern semantic projections and offline previews."""

from podgen.writing import prompt_cache
from podgen.writing.author_reply import offered, wire_schema
from podgen.writing.model_references import bind_schema, preview_references
from podgen.writing.projection_versions import CURRENT
from podgen.writing.projections import role_payload, validate_model_content

from .compiler import response_request


def compile_role_request(
    run_id,
    stage,
    profile,
    materials,
    instructions,
    schema,
    *,
    projection_version=CURRENT,
    reference_catalog=None,
    prompt_cache_layout=None,
    **inputs,
):
    prompt_cache.validate(prompt_cache_layout, projection_version)
    payload = role_payload(stage, materials, projection_version=projection_version, **inputs)
    references = reference_catalog or preview_references(materials, projection_version)
    if references is not None:
        payload = references.translate(payload)
        schema = bind_schema(schema, references)
    if offered(stage, projection_version):
        schema = wire_schema(schema, speaker_labels=materials.speaker_labels)
    validate_model_content(dict(instructions=instructions, schema=schema), materials.documents)
    body = response_request(
        run_id, stage, profile, payload, instructions, schema, prompt_cache_layout=prompt_cache_layout
    )
    return body


def compile_reading_request(
    run_id,
    stage,
    profile,
    materials,
    instructions,
    schema,
    *,
    reference_catalog=None,
    revision_comparison=None,
    prompt_cache_layout=None,
    **inputs,
):
    from podgen.writing.reading_views import reading_payload

    from .reading_workflow import base_stage

    payload = reading_payload(base_stage(stage), materials, prompt_cache_layout=prompt_cache_layout, **inputs)
    if revision_comparison is not None:
        from podgen.writing.revision.verification import STAGES, bind_assignments

        if stage not in STAGES:
            from podgen.domain.errors import ContractError

            raise ContractError("revision_verification_stage_mismatch")
        payload["revision_check"] = revision_comparison
        schema = bind_assignments(schema, revision_comparison)
    references = reference_catalog or preview_references(materials, inputs.get("projection_version", CURRENT))
    if references is not None:
        payload = references.translate(payload)
        schema = bind_schema(schema, references)
    if offered(stage, inputs.get("projection_version", CURRENT)):
        schema = wire_schema(schema, speaker_labels=materials.speaker_labels)
    return response_request(
        run_id, stage, profile, payload, instructions, schema, prompt_cache_layout=prompt_cache_layout
    )
