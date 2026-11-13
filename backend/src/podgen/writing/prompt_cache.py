"""A run pin for reusable request prefixes and stable revision ID schemas."""

from podgen.domain.errors import ContractError

from .projection_versions import V9, manifest_version

KEY = "prompt_cache_layout"
VERSION = "stable-role-prefix/1"
RESEARCH_VERSION = "research-role-prefix/2"
VERSIONS = (VERSION, RESEARCH_VERSION)


def validate(value, projection=V9):
    if value is not None and (value not in VERSIONS or projection != V9):
        raise ContractError("unsupported_prompt_cache_layout")
    return value


def from_manifest(manifest):
    value = validate(manifest.get(KEY), manifest_version(manifest))
    if value is not None and manifest.get("behavior_profile") != "production/1":
        raise ContractError("unsupported_prompt_cache_layout")
    return value


def for_materials(default, materials):
    if (
        default is not None
        and getattr(getattr(materials, "packet", None), "contract", None) == "research-editorial-input/1"
    ):
        return RESEARCH_VERSION
    return default
