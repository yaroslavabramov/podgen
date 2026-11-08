"""Reconstruct pre-addressed manifests completely, without weakening production checks."""

from copy import deepcopy

from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.model_references import CATALOG_KEY, ModelReferences, bind_schema
from podgen.writing.projection_versions import V8


def historical_revision_manifest(value, *, projection_version=V8):
    manifest = deepcopy(value)
    manifest["role_projection_version"] = projection_version
    for field in (
        "writer_response_contract",
        "plan_selection_contract",
        "planning_output_contract",
        "planning_duration_contract",
        "script_revision_contract",
        "revision_context_contract",
        "revision_response_schemas",
        "fact_review_schema_version",
    ):
        manifest.pop(field, None)
    references = ModelReferences.restore(manifest[CATALOG_KEY]) if CATALOG_KEY in manifest else None
    for stage in manifest["response_schemas"]:
        manifest["response_schemas"][stage] = bind_schema(
            response_schema(
                stage,
                manifest["input_contract"],
                projection_version=projection_version,
                reader_schema_version=manifest.get("reader_review_schema_version"),
            ),
            references,
        )
    return manifest
