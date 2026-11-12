"""Credentials never enter frozen requests; authentication is origin-bound."""

from podgen.audio.http import RawAudioTransport
from podgen.domain.errors import ContractError

from .contracts import ReviewConfigV2


class ReviewerTransport:
    def __init__(self, config, *, client=None):
        self.routes = {
            "https://generativelanguage.googleapis.com/v1beta/interactions": RawAudioTransport(
                timeout_seconds=config.reviewer.request_timeout_seconds,
                auth_header="x-goog-api-key",
                auth_prefix="",
                client=client,
            )
        }
        if isinstance(config, ReviewConfigV2):
            self.routes["https://api.openai.com/v1/chat/completions"] = RawAudioTransport(
                timeout_seconds=config.fallback.reviewer.request_timeout_seconds,
                client=client,
            )

    def submit(self, request, wire, credential):
        transport = self.routes.get(request["url"])
        if transport is None:
            raise ContractError("review_unrecognized_provider_endpoint")
        return transport.submit(request, wire, credential)
