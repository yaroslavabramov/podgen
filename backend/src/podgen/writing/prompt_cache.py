"""A run pin for reusable request prefixes and stable revision ID schemas."""

from podgen.domain.errors import ContractError

from .projection_versions import V9, manifest_version

KEY = "prompt_cache_layout"
VERSION = "stable-role-prefix/1"


def validate(value, projection=V9):
    if value is not None and (value != VERSION or projection != V9):
        raise ContractError("unsupported_prompt_cache_layout")
    return value


def from_manifest(manifest):
    value = validate(manifest.get(KEY), manifest_version(manifest))
    if value is not None and manifest.get("behavior_profile") != "production/1":
        raise ContractError("unsupported_prompt_cache_layout")
    return value
