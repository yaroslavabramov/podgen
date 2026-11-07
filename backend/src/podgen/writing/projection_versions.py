"""A run pins role views and model-facing schema semantics separately from persisted data."""

from podgen.domain.errors import ContractError

V1 = "editorial-role-projections/1"
V2 = "editorial-role-projections/2"
V3 = "editorial-role-projections/3"
V4 = "editorial-role-projections/4"
V5 = "editorial-role-projections/5"
V6 = "editorial-role-projections/6"
V7 = "editorial-role-projections/7"
V8 = "editorial-role-projections/8"
CURRENT = V8
PREVIOUS = V1  # Compatibility for callers that imported the former name.


def validate_version(value):
    if value not in (V1, V2, V3, V4, V5, V6, V7, V8):
        raise ContractError("unknown_role_projection_version")
    return value


def manifest_version(manifest):
    return validate_version(manifest.get("role_projection_version", V1))


def historical(version):
    validate_version(version)
    return version in (V1, V2, V3)


def creative_materials(version):
    validate_version(version)
    return version in (V5, V6, V7, V8)


def script_handoff(version):
    validate_version(version)
    return version in (V7, V8)
