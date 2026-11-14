"""Authenticate an old direct count without rewriting its historical evidence."""

from dataclasses import dataclass

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.contracts.configuration import ModelProfile
from podgen.domain.errors import Conflict
from podgen.runtime.evaluation.compiler import count_request
from podgen.runtime.evaluation.validation import load_unique_json

from .count_receipts import identity, native_count, provider_configuration
from .legacy_count_receipts import LegacyExactCount


@dataclass(frozen=True)
class LegacyDirectCount(LegacyExactCount):
    intent_reference: RuntimeRef

    def validate(self, journal, body, profile):
        record = journal.read(self.reference)
        data = record["data"]
        if (
            self.identity != identity(body, profile)
            or record["event_kind"] != "count_response"
            or "count_contract" in data
            or not 200 <= record.get("provider_http_status", 0) < 300
            or record.get("secret_redacted", False)
            or record.get("wire_request_blob") is None
            or type(self.input_tokens) is not int
            or not 0 <= self.input_tokens <= 2**63 - 1
        ):
            raise Conflict("legacy_count_receipt_binding_unproven")
        intent = journal.read(self.intent_reference)
        if (
            intent["event_kind"] != "count_intent"
            or intent["invocation_id"] != record["invocation_id"]
            or intent["request_ref"] != record["request_ref"]
            or any(
                intent["data"].get(key) != data.get(key)
                for key in ("source_turn_id", "source_turn_index", "turn_request_ref")
            )
        ):
            raise Conflict("legacy_count_receipt_intent_unproven")
        intended = _read(journal, self.intent_reference, intent["data"]["request_blob"])
        if digest(intended) != digest(count_request(body)):
            raise Conflict("legacy_count_receipt_request_changed")
        request_ref = RuntimeRef.model_validate(data.get("turn_request_ref", record["request_ref"]))
        request = journal.read(request_ref)
        if (
            request.get("invocation_id") != data.get("source_turn_id", record["invocation_id"])
            or digest(_read(journal, request_ref, request["request_blob"])) != digest(body)
        ):
            raise Conflict("legacy_count_receipt_request_changed")
        profile_value = request.get("context", {}).get("profile_ref")
        if profile_value is None:
            raise Conflict("legacy_count_receipt_profile_unproven")
        profile_ref = ArtifactRef.model_validate(profile_value)
        if profile_ref.kind != "ModelProfile":
            raise Conflict("legacy_count_receipt_profile_unproven")
        archived = ModelProfile.model_validate(journal.repo.get(journal.project, profile_ref).payload)
        if digest(provider_configuration(archived)) != digest(provider_configuration(profile)):
            raise Conflict("legacy_count_receipt_configuration_changed")
        if digest(_read(journal, self.reference, record["wire_request_blob"])) != digest(
            native_count(body, profile.provider)
        ):
            raise Conflict("legacy_count_receipt_native_request_changed")
        response = _read(journal, self.reference, record["response_blob"])
        if type(response.get("input_tokens")) is not int or response["input_tokens"] != self.input_tokens:
            raise Conflict("legacy_count_receipt_response_changed")
        return self.input_tokens


def _read(journal, reference, blob):
    return load_unique_json(journal.read_blob(reference, BlobRef.model_validate(blob)))
