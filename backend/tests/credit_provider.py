"""One controlled submit refusal, with the real provider's archived error contract."""

import json

from podgen.runtime.models.contracts import ProviderFault, WireReply


class CreditProvider:
    def __init__(
        self,
        inner,
        *,
        stage="review",
        operation="role",
        remaining=1,
        status=429,
        code="credit_balance_exhausted",
        ambiguous=False,
        body=None,
    ):
        self.inner = inner
        self.stage, self.operation, self.remaining = stage, operation, remaining
        self.status, self.code, self.ambiguous, self.body = status, code, ambiguous, body
        self.requests = inner.requests
        self.counts = inner.counts

    def count(self, body):
        return self.inner.count(body)

    def submit(self, body, *, client_request_id):
        operation = "anchor_repair" if body["text"]["format"]["name"] == "anchor_choices" else "role"
        if body["metadata"]["podgen_stage"] == self.stage and operation == self.operation and self.remaining:
            self.requests.append(body)
            self.remaining -= 1
            error = (
                self.body
                if self.body is not None
                else json.dumps(dict(error=dict(code=self.code, message="SYNTHETIC credit refusal"))).encode()
            )
            raise ProviderFault(
                f"provider_http_{self.status}",
                ambiguous=self.ambiguous,
                reply=WireReply(
                    error,
                    self.status,
                    "synthetic-request-id",
                    request_body=json.dumps(body, ensure_ascii=False).encode(),
                ),
            )
        return self.inner.submit(body, client_request_id=client_request_id)

    def retrieve(self, response_id):
        return self.inner.retrieve(response_id)
