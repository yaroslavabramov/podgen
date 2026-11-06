"""One compiler boundary for all modern semantic projections and offline previews."""

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
    **inputs,
):
    payload = role_payload(stage, materials, projection_version=projection_version, **inputs)
    references = reference_catalog or preview_references(materials, projection_version)
    if references is not None:
        payload = references.translate(payload)
        schema = bind_schema(schema, references)
    validate_model_content(dict(instructions=instructions, schema=schema), materials.documents)
    body = response_request(run_id, stage, profile, payload, instructions, schema)
    return body


def compile_reading_request(
    run_id, stage, profile, materials, instructions, schema, *, reference_catalog=None, **inputs
):
    from podgen.writing.reading_views import reading_payload

    from .reading_workflow import base_stage

    payload = reading_payload(base_stage(stage), materials, **inputs)
    references = reference_catalog or preview_references(materials, inputs.get("projection_version", CURRENT))
    if references is not None:
        payload = references.translate(payload)
        schema = bind_schema(schema, references)
    return response_request(run_id, stage, profile, payload, instructions, schema)
