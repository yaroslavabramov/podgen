"""Version the lossless sharing of current packet evidence with previous audit decisions."""

from podgen.domain.errors import ContractError

from . import KEY as AUDIT_KEY
from . import VERSION as AUDIT_VERSION

KEY = "reading_audit_memory_projection"
VERSION = "shared-packet-evidence/1"
STABLE_REFERENCES = "shared-packet-evidence/2"


def from_manifest(manifest):
    value = manifest.get(KEY)
    if value is not None and (
        value not in (VERSION, STABLE_REFERENCES) or manifest.get(AUDIT_KEY) != AUDIT_VERSION
    ):
        raise ContractError("unsupported_reading_audit_memory_projection")
    return value
