"""Authenticate historical context counts using their original wire and pinned profile."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.contracts.configuration import ModelProfile
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.validation import load_unique_json

from .count_receipts import ExactCount, identity, native_count, provider_configuration


class LegacyExactCount(ExactCount):
    def validate(self, journal, body, profile):
        record = journal.read(self.reference)
        data = record["data"]
        if (
            self.identity != identity(body, profile)
            or record["event_kind"] != "source_context_count"
            or "count_contract" in data
            or data.get("request_hash") != digest(body)
            or type(data.get("input_tokens")) is not int
            or data["input_tokens"] < 0
            or data["input_tokens"] != self.input_tokens
            or not 200 <= record.get("provider_http_status", 0) < 300
            or record.get("secret_redacted", False)
            or record.get("wire_request_blob") is None
            or record.get("response_blob") is None
        ):
            raise Conflict("legacy_count_receipt_binding_unproven")
        request_ref = RuntimeRef.model_validate(record["request_ref"])
        request = journal.read(request_ref)
        profile_ref = request.get("context", {}).get("profile_ref")
        if request.get("invocation_id") != record["invocation_id"] or profile_ref is None:
            raise Conflict("legacy_count_receipt_profile_unproven")
        profile_ref = ArtifactRef.model_validate(profile_ref)
        if profile_ref.kind != "ModelProfile":
            raise Conflict("legacy_count_receipt_profile_unproven")
        archived = journal.repo.get(journal.project, profile_ref).payload
        if digest(provider_configuration(ModelProfile.model_validate(archived))) != digest(
            provider_configuration(profile)
        ):
            raise Conflict("legacy_count_receipt_configuration_changed")
        wire = load_unique_json(
            journal.read_blob(self.reference, BlobRef.model_validate(record["wire_request_blob"]))
        )
        if digest(wire) != digest(native_count(body, profile.provider)):
            raise Conflict("legacy_count_receipt_native_request_changed")
        response = load_unique_json(
            journal.read_blob(self.reference, BlobRef.model_validate(record["response_blob"]))
        )
        if type(response.get("input_tokens")) is not int or response["input_tokens"] != self.input_tokens:
            raise Conflict("legacy_count_receipt_response_changed")
        return self.input_tokens

    def register(self, journal, phase):
        """Validation ran on full historical native bytes; save only its private proof."""
        from podgen.persistence.database import json_text, query

        payload = dict(count_identity=self.identity, input_tokens=self.input_tokens)

        def write(tx):
            query(
                tx,
                """INSERT INTO operational_records
                (project_id,record_kind,record_id,parent_record_id,payload)
                VALUES(:p,'legacy_exact_count',:id,NULL,CAST(:body AS jsonb))
                ON CONFLICT DO NOTHING""",
                p=journal.project,
                id=self.reference.record_id,
                body=json_text(payload),
            )

        phase.proofs.append(write)


def receipt(journal, entry, body, profile):
    if entry is None or entry.data.get("count_identity"):
        return None
    result = LegacyExactCount(entry.reference, identity(body, profile), entry.data["input_tokens"])
    try:
        result.validate(journal, body, profile)
    except Conflict:
        # An unbound historical count is not proof. Obtain a new bound receipt.
        return None
    return result
