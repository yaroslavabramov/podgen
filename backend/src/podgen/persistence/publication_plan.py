"""Bounded immutable publication inputs; identity resolution precedes substitution."""

from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.domain.packages import substitute


@dataclass(frozen=True)
class ArtifactNode:
    key: str
    kind: str
    payload: dict
    producer: object
    operation_id: object
    input_refs: tuple = ()
    input_keys: tuple = ()
    artifact_id: object = None
    identity_of: str | None = None
    episode_id: object = None
    audience: str = "project"
    streaming: bool = False
    allocated_artifact_id: object = field(default_factory=uuid4)
    reference_policy: str = "legacy/1"
    revision_id: object = field(default_factory=uuid4)


@dataclass(frozen=True)
class RuntimeNode:
    key: str
    ref: RuntimeRef
    payload: dict
    audience: str = "project"
    replay: bool = True
    journal: object = None


@dataclass
class PublicationBatch:
    artifacts: tuple = ()
    runtimes: tuple = ()
    external_artifacts: dict = field(default_factory=dict)
    external_runtimes: dict = field(default_factory=dict)
    blobs: dict = field(default_factory=dict)
    prepared: object = None
    root: Path | None = None

    def validate_size(self):
        nodes = (*self.artifacts, *self.runtimes)
        if not nodes:
            raise ContractError("invalid_publication_size")
        keys = [node.key for node in nodes] + list(self.external_artifacts) + list(self.external_runtimes)
        if len(set(keys)) != len(keys) or len({n.operation_id for n in self.artifacts}) != len(
            self.artifacts
        ):
            raise ContractError("duplicate_publication_identity")
        if len({n.ref.record_id for n in self.runtimes}) != len(self.runtimes):
            raise ContractError("duplicate_runtime_publication_identity")

    def body(self, payload, artifacts, runtimes):
        return substitute(payload, artifacts, runtimes, self.blobs, self.root)


class PublicationResult(dict):
    """Committed values and canonical runtime bodies owned by this operation."""

    def __init__(self, values, payloads):
        super().__init__(values)
        self.payloads = payloads


@dataclass(frozen=True)
class PublicationIdentities:
    """Exact identity query result consumed inside its original SQL scope."""

    transaction: object
    scope: tuple
    project: object
    runtime_ids: frozenset
    runtimes: dict

    def require(self, tx, project, identities):
        if (
            self.transaction is not tx
            or self.project != project
            or self.scope != (tx.get_transaction(), tx.get_nested_transaction())
            or not set(identities) <= self.runtime_ids
        ):
            raise ContractError("publication_identity_scope_mismatch")
        return self.runtimes
