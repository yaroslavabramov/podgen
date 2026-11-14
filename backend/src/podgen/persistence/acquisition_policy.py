"""Closed acquisition journal v2 references; JSON cannot choose permission flags."""

from dataclasses import dataclass
from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.domain.references import walk_values

VERSION = "acquisition-journal/2"
SOURCE = "source-document/2"


@dataclass(frozen=True)
class JournalBinding:
    operation_id: UUID
    stage: str
    role: str = ""

    def key(self):
        return self.stage if not self.role else self.role + ":" + self.stage

    def reference(self):
        return RuntimeRef(record_kind="acquisition_record", record_id=uuid5(self.operation_id, self.key()))

    def wire(self):
        return dict(operation_id=str(self.operation_id), stage=self.stage, role=self.role)


def projection(binding, ref, payload):
    if not isinstance(binding, JournalBinding) or ref != binding.reference():
        raise ContractError("acquisition_journal_identity_mismatch")
    if binding.stage not in ("request", "original", "extracted", "outcome", "result"):
        raise ContractError("acquisition_journal_stage_unknown")
    from .acquisition_contracts import validate

    validate(binding, payload)
    values = []
    for _, path, linked in walk_values(payload):
        read, privacy, owned = False, False, False
        if binding.stage == "request":
            if (
                path == "/profile_ref"
                and isinstance(linked, ArtifactRef)
                and linked.kind == "ProcessingProfile"
            ):
                pass
            elif (
                path in ("/rights/storage_policy_ref", "/rights/publication_policy_ref")
                and isinstance(linked, ArtifactRef)
                and linked.kind == "Policy"
            ):
                privacy = True
            elif (
                path == "/source_revision_ref"
                and isinstance(linked, ArtifactRef)
                and linked.kind == "SourceRevision"
            ):
                privacy = True  # Explicit re-extraction retains original content provenance.
            else:
                raise ContractError("acquisition_journal_reference_unknown", path)
        elif binding.stage in ("original", "extracted"):
            if path in (
                ("/request_ref",) if binding.stage == "original" else ("/original_ref", "/request_ref")
            ) and isinstance(linked, RuntimeRef):
                expected = JournalBinding(binding.operation_id, "request")
                if path == "/request_ref" and linked != expected.reference():
                    raise ContractError("acquisition_journal_parent_mismatch")
                privacy = True
            elif path == ("/original_blob" if binding.stage == "original" else "/text_blob") and isinstance(
                linked, BlobRef
            ):
                read = privacy = owned = True
            else:
                raise ContractError("acquisition_journal_reference_unknown", path)
        else:
            prefix = "/source/" if binding.stage == "outcome" else "/sources/"
            if (
                isinstance(linked, ArtifactRef)
                and path.startswith(prefix)
                and (path.endswith("/source_revision_ref") or "/representation_refs/" in path)
            ):
                expected = (
                    "SourceRevision" if path.endswith("/source_revision_ref") else "SourceRepresentation"
                )
                if linked.kind != expected:
                    raise ContractError("acquisition_journal_output_kind_mismatch", path)
                read = privacy = True
            elif isinstance(linked, RuntimeRef) and (
                path == "/acquisition_record_ref"
                or path.startswith(prefix)
                and path.endswith("/acquisition_record_ref")
            ):
                privacy = True
            else:
                raise ContractError("acquisition_journal_reference_unknown", path)
        if read or privacy:
            values.append(
                dict(
                    kind="artifact"
                    if isinstance(linked, ArtifactRef)
                    else "runtime"
                    if isinstance(linked, RuntimeRef)
                    else "blob",
                    ref=linked.model_dump(mode="json"),
                    read=read,
                    privacy=privacy,
                    owned=owned,
                )
            )
    return values


def fingerprint(binding, ref, payload):
    return digest(
        dict(policy=VERSION, binding=binding.wire(), ref=ref.model_dump(mode="json"), payload=payload)
    )


def semantic_inputs(binding, payload):
    """Request inputs govern execution; control parents have direct ownership checks."""
    if binding.stage == "request":
        return [ref for _, _, ref in walk_values(payload)]
    return [
        linked
        for item in projection(binding, binding.reference(), payload)
        if item["read"]
        for _, _, linked in walk_values(item["ref"])
    ]
