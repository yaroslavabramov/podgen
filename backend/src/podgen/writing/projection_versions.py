"""A run pins role views and model-facing schema semantics separately from persisted data."""

from podgen.domain.errors import ContractError

PREVIOUS = "editorial-role-projections/1"
CURRENT = "editorial-role-projections/2"


def validate_version(value):
    if value not in (PREVIOUS, CURRENT):
        raise ContractError("unknown_role_projection_version")
    return value


def manifest_version(manifest):
    return validate_version(manifest.get("role_projection_version", PREVIOUS))
