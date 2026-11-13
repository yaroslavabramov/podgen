"""Synchronous Messages transport. Physical traffic and provider accounting are journaled."""

import json
from contextlib import nullcontext

import httpx

from .contracts import ProviderFault, WireReply
from .messages_versions import CURRENT_API, LEGACY_API, validate_wire
from .messages_wire import messages_request, normalize_message


class AnthropicProvider:
    def __init__(self, api_key, *, timeout_seconds=1800, http_client=None, api_version=CURRENT_API):
        if not api_key:
            raise ProviderFault("missing_anthropic_api_key")
        if api_version not in (CURRENT_API, LEGACY_API):
            raise ProviderFault("unsupported_messages_api_version")
        self._api_version = api_version
        self._key = api_key
        self._client = http_client
        self._timeout = timeout_seconds

    def _call(self, body, *, client_request_id=None):
        counting = client_request_id is None
        payload = messages_request(body, counting=counting)
        validate_wire(payload, self._api_version)
        client = (
            nullcontext(self._client) if self._client is not None else httpx.Client(timeout=self._timeout)
        )
        url = "https://api.anthropic.com/v1/messages" + ("/count_tokens" if counting else "")
        headers = {"anthropic-version": self._api_version, "x-api-key": self._key}
        if client_request_id is not None:
            headers["x-client-request-id"] = str(client_request_id)
        with client as http:
            request = http.build_request("POST", url, headers=headers, json=payload)
            secret = self._key.encode()
            sent = request.content.replace(secret, b"[REDACTED_API_KEY]")
            try:
                reply = http.send(request)
            except httpx.TransportError:
                raise ProviderFault(
                    "provider_transport_error" if counting else "provider_transport_outcome_unknown",
                    ambiguous=not counting,
                    reply=WireReply(b"", 0, request_body=sent),
                ) from None
        received = reply.content.replace(secret, b"[REDACTED_API_KEY]")
        physical = WireReply(
            received,
            reply.status_code,
            reply.headers.get("request-id"),
            received != reply.content or sent != request.content,
            sent,
        )
        if not reply.is_success:
            raise ProviderFault(
                f"provider_http_{reply.status_code}",
                ambiguous=not counting and (reply.status_code >= 500 or reply.status_code == 408),
                reply=physical,
            )
        if counting:
            return physical
        try:
            envelope = normalize_message(json.loads(received), body)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderFault("provider_malformed_envelope", ambiguous=True, reply=physical) from None
        return WireReply(
            json.dumps(envelope, ensure_ascii=False).encode(),
            physical.status_code,
            physical.request_id,
            physical.redacted,
            sent,
            wire_response=received,
        )

    def count(self, body):
        return self._call(body)

    def submit(self, body, *, client_request_id):
        return self._call(body, client_request_id=client_request_id)

    def retrieve(self, response_id):
        raise ProviderFault("response_not_retrievable")
