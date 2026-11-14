"""Complete acquisition receipt payload contracts, separate from reference policy."""

from typing import Literal

from pydantic import Field, JsonValue, ValidationError, model_validator

from podgen.domain.contracts.common import BlobRef, Contract, RuntimeRef, Text
from podgen.domain.errors import ContractError
from podgen.sources.acquisition.contracts import (
    AcquiredSource,
    AcquisitionRequest,
    AcquisitionResult,
    ReExtractionRequest,
)

ROLES = ("original", "mediawiki", "jina", "promoted_pdf", "browser")


class OriginalReceipt(Contract):
    contract: Literal["source-received-original/1"]
    request_ref: RuntimeRef
    role: Literal["original", "mediawiki", "jina", "promoted_pdf", "browser"]
    original_blob: BlobRef
    requested_url: Text
    resolved_url: Text
    redirect_chain: list[dict[str, JsonValue]]
    status_code: int
    headers: dict[str, str]
    acquired_at: Text
    acquisition_kind: Literal["http", "browser_snapshot", "upload", "connector"]
    limitations: list[str]


class ExtractionMetadata(Contract):
    name: Text
    version: Text
    title: str | None = None
    published_date: str | None = None
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    warnings: list[str] = Field(default_factory=list)
    blocks: list[dict[str, JsonValue]] = Field(default_factory=list)
    is_transformation: bool = False


class ExtractedReceipt(Contract):
    request_ref: RuntimeRef
    original_ref: RuntimeRef
    text_blob: BlobRef
    metadata: ExtractionMetadata


class DocumentOutcome(Contract):
    source: AcquiredSource | None
    diagnostics: list[str] | None = None

    @model_validator(mode="after")
    def failure_diagnostics(self):
        if self.source is None and self.diagnostics is None:
            raise ValueError("failed fetch requires diagnostics")
        return self


def validate(binding, payload):
    if (binding.stage in ("request", "result")) != (binding.role == "") or (
        binding.role and binding.role not in ROLES
    ):
        raise ContractError("acquisition_journal_role_invalid")
    models = dict(
        original=OriginalReceipt,
        extracted=ExtractedReceipt,
        outcome=DocumentOutcome,
        result=AcquisitionResult,
    )
    try:
        if binding.stage == "request":
            model = (
                ReExtractionRequest
                if payload.get("contract") == "source-reextraction-request/1"
                else AcquisitionRequest
            )
        else:
            model = models[binding.stage]
        value = model.model_validate(payload)
        if binding.stage == "request" and value.operation_id != binding.operation_id:
            raise ValueError("request owner mismatch")
        if binding.stage == "original" and value.role != binding.role:
            raise ValueError("original role mismatch")
        if binding.stage == "outcome" and value.source is not None and value.source.role != binding.role:
            raise ValueError("outcome role mismatch")
    except (ValidationError, KeyError, ValueError, TypeError) as error:
        raise ContractError("acquisition_journal_payload_invalid", str(error)) from error


def extraction_receipt(repo, tx, project, row, original_ref, original):
    """Adopt a legacy receipt without rewriting its missing v2 control fields."""
    if row.get("reference_policy", "legacy/1") == "legacy/1":
        from uuid import UUID

        from .acquisition_policy import JournalBinding

        request = original.request_ref
        # Historical stage IDs are bound to the original's operation, not to a
        # guessed operation from arbitrary metadata or the new reprocess run.
        from .database import one

        owner = one(
            tx,
            "SELECT payload FROM runtime_records WHERE project_id=:p AND record_id=:r",
            p=project,
            r=request.record_id,
        )
        if owner is None:
            raise ContractError("source_extraction_receipt_mismatch")
        expected = JournalBinding(
            UUID(owner["payload"]["operation_id"]), "extracted", original.role
        ).reference()
        actual = RuntimeRef(record_kind=row["record_kind"], record_id=row["record_id"])
        if actual != expected:
            raise ContractError("source_extraction_receipt_mismatch")
        repo._runtime(tx, project, actual)  # Preserve all legacy read obligations.
        payload = row["payload"]
        if set(payload) != {"text_blob", "metadata"}:
            raise ContractError("source_extraction_receipt_mismatch")
        return ExtractedReceipt.model_validate(
            payload
            | dict(
                request_ref=request.model_dump(mode="json"), original_ref=original_ref.model_dump(mode="json")
            )
        )
    return ExtractedReceipt.model_validate(row["payload"])
