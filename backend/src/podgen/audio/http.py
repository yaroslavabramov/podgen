"""Single raw HTTP exchange shared by audio provider ports; no retry policy."""

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class HttpReceipt:
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    complete: bool = True
    error: str | None = None


class RawAudioTransport:
    def __init__(
        self, *, timeout_seconds=900, client=None, auth_header="authorization", auth_prefix="Bearer "
    ):
        self.timeout_seconds, self.client = timeout_seconds, client
        self.auth_header, self.auth_prefix = auth_header, auth_prefix

    def submit(self, request, wire_body, credential):
        started = time.monotonic()
        headers = dict(request["headers"], **{self.auth_header: self.auth_prefix + credential})
        client = self.client or httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=10),
            follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0),
        )
        chunks, status, response_headers = [], 0, {}
        try:
            with client.stream("POST", request["url"], headers=headers, content=wire_body) as response:
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
