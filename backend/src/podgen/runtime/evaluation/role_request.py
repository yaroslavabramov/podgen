"""One compiler boundary for all modern semantic projections and offline previews."""

from podgen.writing.projection_versions import CURRENT
from podgen.writing.projections import role_payload, validate_model_content

from .compiler import response_request


def compile_role_request(
    run_id, stage, profile, materials, instructions, schema, *, projection_version=CURRENT, **inputs
):
    payload = role_payload(stage, materials, projection_version=projection_version, **inputs)
    validate_model_content(dict(instructions=instructions, schema=schema), materials.documents)
    body = response_request(run_id, stage, profile, payload, instructions, schema)
    return body


def compile_reading_request(run_id, stage, profile, materials, instructions, schema, **inputs):
    from podgen.writing.reading_views import reading_payload

    from .reading_workflow import base_stage

    payload = reading_payload(base_stage(stage), materials, **inputs)
    return response_request(run_id, stage, profile, payload, instructions, schema)
