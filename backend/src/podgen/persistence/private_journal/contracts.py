"""Typed checkpoint commands; arbitrary SQL callbacks are not a journal contract."""

import hashlib
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import ContractError

NAMESPACE = UUID("43bd5901-a208-5b22-b26c-67292f18eaa0")
ACCOUNTING_FIELDS = frozenset(
    {
        "count_calls",
        "count_reuses",
        "generation_calls",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_microusd",
        "output_reserved",
        "rejected_calls",
        "uncertain_calls",
    }
)


def operation_id(step_id, operation, *identity):
    return uuid5(step_id, f"private/3/{operation}/{digest(list(identity))}")


def commit_identity(run_id, kind, operation, observation=0):
    return digest(
        dict(
            storage="private/3",
            run=str(run_id),
            kind=kind,
            operation=str(operation),
            observation=str(observation),
        )
    )


@dataclass(frozen=True)
class PrivateBytes:
    reference: BlobRef
    body: bytes

    def __post_init__(self):
        if not isinstance(self.body, bytes) or (
            len(self.body) != self.reference.byte_length
            or hashlib.sha256(self.body).hexdigest() != self.reference.sha256
        ):
            raise ContractError("private_object_content_mismatch")


@dataclass(frozen=True)
class PrivateObjectUse:
    reference: BlobRef
    owner_entry_id: UUID


@dataclass(frozen=True)
class ChargeChange:
    operation_id: UUID
    expected_revision: int
    old: dict
    new: dict
    evidence_entry_id: UUID


@dataclass(frozen=True)
class SemanticResult:
    reference: RuntimeRef
    payload: dict
    source_entries: tuple[UUID, ...]


@dataclass(frozen=True)
class HeadChange:
    phase: str | None = None
    request_entry_id: UUID | None = None
    provider_entry_id: UUID | None = None
    submit_entry_id: UUID | None = None
    pending_parent_entry_id: UUID | None = None
    request_hash: str | None = None
    configuration_hash: str | None = None
    count_identity: str | None = None
    count_entry_id: UUID | None = None
    provider_id: str | None = None
    accounting_delta: dict = field(default_factory=dict)
    preserve_active: bool = False
    clear_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Checkpoint:
    run_id: UUID
    attempt_id: UUID
    operation_id: UUID
    kind: str
    input_set_id: UUID
    expected_revision: int
    data: dict
    step_id: UUID | None = None
    step_index: int | None = None
    observation_no: int = 0
    views: tuple = ()
    objects: dict = field(default_factory=dict)
    parents: tuple = ()
    head: HeadChange = field(default_factory=HeadChange)
    action: bool = False
    charges: tuple[ChargeChange, ...] = ()
    semantic_result: SemanticResult | None = None

    @property
    def commit_key(self):
        return commit_identity(self.run_id, self.kind, self.operation_id, self.observation_no)

    @property
    def entry_id(self):
        return uuid5(NAMESPACE, self.commit_key)

    def archive_id(self, slot):
        return uuid5(self.entry_id, f"archive/3/{slot}")
