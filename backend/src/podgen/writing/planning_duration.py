"""Pin the author's existing duration estimate in planning inputs without changing old views."""

from podgen.domain.errors import ContractError

from .projection_versions import V9, manifest_version

KEY = "planning_duration_contract"
VERSION = "planning-duration-context/1"


def for_input(input_contract, projection):
    return VERSION if input_contract == "editorial-role-packets/1" and projection == V9 else None


def validate(version, projection):
    if version is not None and (version != VERSION or projection != V9):
        raise ContractError("unsupported_planning_duration_contract")
    return version


def from_manifest(manifest):
    return validate(manifest.get(KEY), manifest_version(manifest))
