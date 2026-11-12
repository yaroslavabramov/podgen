"""One explicit HTTP submission; partial/lost responses remain visible, never retried."""

import time

import httpx

from podgen.domain.canonical import canonical_bytes

from .contracts import HttpReceipt


def encode_request(request):
    return canonical_bytes(request.body)


AUTH = dict(
    elevenlabs=("xi-api-key", ""),
    gemini=("x-goog-api-key", ""),
    inworld=("authorization", "Basic "),
    fish=("authorization", "Bearer "),
    minimax=("authorization", "Bearer "),
)


class HttpSynthesisTransport:
    def __init__(self, *, client=None, timeout_seconds=180):
        self.client = client
        self.timeout = timeout_seconds

    def submit(self, request, credential):
        key, prefix = AUTH[request.provider]
        headers = dict(request.headers, **{key: prefix + credential, "accept-encoding": "identity"})
        started = time.monotonic()
        chunks, status, response_headers = [], 0, {}
        client = self.client or httpx.Client(
            timeout=self.timeout, follow_redirects=False, transport=httpx.HTTPTransport(retries=0)
        )
        try:
            with client.stream(
                request.method,
                request.url,
                params=request.query,
                headers=headers,
                content=encode_request(request),
            ) as response:
                status = response.status_code
                response_headers = {
                    k.lower(): v for k, v in response.headers.items() if k.lower() != "set-cookie"
                }
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
            return HttpReceipt(
                status, response_headers, b"".join(chunks), round((time.monotonic() - started) * 1000)
            )
        except httpx.TransportError as error:
            return HttpReceipt(
                status,
                response_headers,
                b"".join(chunks),
                round((time.monotonic() - started) * 1000),
                False,
                type(error).__name__,
            )
        finally:
            if self.client is None:
                client.close()
