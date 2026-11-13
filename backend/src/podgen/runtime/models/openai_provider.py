"""OpenAI Responses adapter. SDK transport retries are deliberately disabled."""

import logging
import re

import httpx2
import openai
from openai import OpenAI

from .contracts import ProviderFault, WireReply
from .responses_wire import responses_request


class OpenAIProvider:
    def __init__(
        self, api_key: str, *, timeout_seconds: float = 1800, http_client: httpx2.Client | None = None
    ):
        if not api_key:
            raise ProviderFault("missing_openai_api_key")
        self._secret = api_key.encode()
        logging.getLogger("openai").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=timeout_seconds,
            http_client=http_client,
        )

    def _reply(self, body, status, request_id, request_body=None):
        clean = body.replace(self._secret, b"[REDACTED_API_KEY]")
        clean = re.sub(rb"(?i)(authorization[\s\"\x27:]+bearer\s+)[^\s\"\x27,}]+", rb"\1[REDACTED]", clean)
        return WireReply(
            clean,
            status,
            request_id,
            clean != body or bool(request_body and self._secret in request_body),
            request_body.replace(self._secret, b"[REDACTED_API_KEY]") if request_body else None,
        )

    def _call(self, operation, *, submit=False):
        from podgen.persistence.read_authorization import require_external_boundary

        require_external_boundary()
        try:
            response = operation()
            return self._reply(
                response.content,
                response.status_code,
                response.headers.get("x-request-id"),
                response.http_response.request.content,
            )
        except openai.APIStatusError as error:
            reply = self._reply(
                error.response.content,
                error.status_code,
                error.response.headers.get("x-request-id"),
                error.request.content,
            )
            raise ProviderFault(
                f"provider_http_{error.status_code}",
                ambiguous=submit and (error.status_code >= 500 or error.status_code == 408),
                reply=reply,
            ) from None
        except (openai.APITimeoutError, openai.APIConnectionError, httpx2.TransportError) as error:
            request = getattr(error, "request", None)
            try:
                body = request.content if request else None
            except httpx2.RequestNotRead:
                body = None
            reply = self._reply(b"", 0, None, body) if body else None
            raise ProviderFault(
                "provider_transport_outcome_unknown" if submit else "provider_transport_error",
                ambiguous=submit,
                reply=reply,
            ) from None
        except (ValueError, openai.APIResponseValidationError):
            raise ProviderFault("provider_malformed_envelope", ambiguous=submit) from None

    def count(self, body):
        from .count_receipts import native_count

        args = native_count(body, "openai")
        return self._call(lambda: self.client.responses.input_tokens.with_raw_response.count(**args))

    def submit(self, body, *, client_request_id):
        return self._call(
            lambda: self.client.responses.with_raw_response.create(
                **responses_request(body), extra_headers={"X-Client-Request-Id": client_request_id}
            ),
            submit=True,
        )

    def retrieve(self, response_id):
        return self._call(lambda: self.client.responses.with_raw_response.retrieve(response_id))
