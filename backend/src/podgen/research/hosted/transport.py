"""No automatic POST retry. Partial responses remain available to the journal."""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Response:
    status_code: int | None
    headers: dict
    body: bytes
    complete: bool = True
    error: str | None = None


class HttpTransport:
    def __init__(self, timeout=120, max_response_bytes=64 * 1024 * 1024):
        self.timeout, self.max_response_bytes = timeout, max_response_bytes

    def request(self, method, url, headers, credential, body=None):
        chunks, size, status, response_headers = [], 0, None, {}
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                with client.stream(
                    method, url, headers={**headers, "x-goog-api-key": credential}, json=body
                ) as r:
                    status = r.status_code
                    response_headers = {k: v for k, v in r.headers.items() if k.lower() != "set-cookie"}
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            return Response(
                                status, response_headers, b"".join(chunks), False, "response_too_large"
                            )
                        chunks.append(chunk)
            return Response(status, response_headers, b"".join(chunks))
        except httpx.HTTPError as error:
            return Response(status, response_headers, b"".join(chunks), False, type(error).__name__)
