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
