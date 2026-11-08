"""Model-facing material for the writer acting on two independent readings."""

from podgen.domain.errors import ContractError

from ..projection_versions import V9, manifest_version
from .contracts import VERSION as ADDRESSED_REVISION

KEY = "revision_context_contract"
VERSION = "script-edit-assignment/1"


def from_manifest(manifest):
    value = manifest.get(KEY)
    if value is not None and (
        value != VERSION
        or manifest_version(manifest) != V9
        or manifest.get("script_revision_contract") != ADDRESSED_REVISION
    ):
        raise ContractError("unsupported_revision_context_contract")
    return value
