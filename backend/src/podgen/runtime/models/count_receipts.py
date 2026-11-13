"""Exact native count identity and small handles to durable provider receipts."""

from dataclasses import dataclass

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict

CONTRACT = "exact-native-count/1"

OPENAI_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "reasoning",
        "text",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "truncation",
    }
)


def native_count(body, provider):
    if provider == "anthropic":
        from .messages_wire import messages_request

        return messages_request(body, counting=True)
    if provider == "openai":
        from .responses_wire import responses_request

        return {key: value for key, value in responses_request(body).items() if key in OPENAI_FIELDS}
    raise Conflict("unsupported_count_provider")


def provider_configuration(profile):
    return {
        key: getattr(profile, key, None)
        for key in (
            "provider",
            "api_family",
            "adapter_version",
            "model_revision_or_alias",
            "generation_parameters",
            "context_limit",
            "output_limit",
            "timeout_policy",
        )
    }


def identity(body, profile):
    return digest(
        dict(
            contract=CONTRACT,
            native_request=native_count(body, profile.provider),
            provider=provider_configuration(profile),
        )
    )


@dataclass(frozen=True)
class ExactCount:
    reference: RuntimeRef
    identity: str
    input_tokens: int

    def validate(self, journal, body, profile):
        from podgen.runtime.evaluation.validation import load_unique_json

        if self.identity != identity(body, profile):
            raise Conflict("count_receipt_request_changed")
        record = journal.read(self.reference)
        data = record["data"]
        if (
            record["event_kind"] != "source_context_count"
            or data.get("count_contract") != CONTRACT
            or not 200 <= record.get("provider_http_status", 0) < 300
            or data.get("count_identity") != self.identity
            or type(data.get("input_tokens")) is not int
            or data["input_tokens"] != self.input_tokens
        ):
            raise Conflict("count_receipt_binding_changed")
        native = load_unique_json(
            journal.read_blob(self.reference, BlobRef.model_validate(data["native_request_blob"]))
        )
        if native != native_count(body, profile.provider) or data.get(
            "provider_configuration"
        ) != provider_configuration(profile):
            raise Conflict("count_receipt_native_request_changed")
        intent = journal.read(RuntimeRef.model_validate(data["count_intent_ref"]))
        if (
            intent["event_kind"] != "source_context_count_intent"
            or intent["invocation_id"] != record["invocation_id"]
            or any(
                intent["data"].get(key) != data.get(key)
                for key in (
                    "count_contract",
                    "count_identity",
                    "native_request_blob",
                    "provider_configuration",
                )
            )
        ):
            raise Conflict("count_receipt_intent_changed")
        if record.get("wire_request_blob") is not None and not record.get("secret_redacted", False):
            wire = load_unique_json(
                journal.read_blob(self.reference, BlobRef.model_validate(record["wire_request_blob"]))
            )
            if wire != native:
                raise Conflict("count_receipt_wire_changed")
        reply = load_unique_json(
            journal.read_blob(self.reference, BlobRef.model_validate(record["response_blob"]))
        )
        if type(reply.get("input_tokens")) is not int or reply["input_tokens"] != self.input_tokens:
            raise Conflict("count_receipt_response_changed")
        return self.input_tokens
