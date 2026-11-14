"""Verified byte ownership for a checkpoint, distinct from its input BlobRefs."""

import hashlib
from dataclasses import dataclass

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import ContractError


@dataclass(frozen=True)
class OwnedBlob:
    reference: BlobRef
    body: bytes

    def __post_init__(self):
        if (
            len(self.body) != self.reference.byte_length
            or hashlib.sha256(self.body).hexdigest() != self.reference.sha256
        ):
            raise ContractError("execution_owned_bytes_mismatch")
