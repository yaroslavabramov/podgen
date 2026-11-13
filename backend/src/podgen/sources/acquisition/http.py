"""Streaming bounded HTTP acquisition; extraction never performs another fetch."""

import asyncio
import zlib
from urllib.parse import urljoin, urlparse

import httpx

from podgen.persistence.repository import timestamp

from .contracts import FetchedDocument
from .network import GuardedTransport, _check_ssrf


class ByteLimitExceeded(ValueError):
    pass


def origin(url):
    p = urlparse(url)
    return p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80)


def decoded_body(document, maximum):
    encoding = document.headers.get("content-encoding", "identity").lower()
    raw = document.body
    if encoding in ("identity", ""):
        if len(raw) > maximum:
            raise ByteLimitExceeded("source_decoded_byte_limit")
        return raw
    if encoding not in ("gzip", "deflate"):
        raise ValueError("source_unsupported_content_encoding:" + encoding)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    result = decoder.decompress(raw, maximum + 1)
    if len(result) > maximum or decoder.unconsumed_tail:
        raise ByteLimitExceeded("source_decoded_byte_limit")
    if not decoder.eof or decoder.unused_data:
        raise ValueError("source_invalid_or_multiple_compressed_streams")
    return result


class HttpSourceFetcher:
    def __init__(self, *, transport=None):
        self.transport = transport

    async def fetch(self, url, profile, *, headers=None):
        request_headers = {
            "User-Agent": profile.user_agent,
            "Accept-Language": profile.accept_language,
            "Accept-Encoding": "identity",
            "Accept": "text/html,application/pdf,*/*;q=0.8",
        }
        request_headers.update(headers or {})
        async with asyncio.timeout(profile.timeout_seconds):
            async with httpx.AsyncClient(
                timeout=profile.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport or GuardedTransport(),
            ) as client:
                current, redirects = url, []
                for hop in range(profile.max_redirects + 1):
                    _check_ssrf(current)
                    async with client.stream("GET", current, headers=request_headers) as response:
                        response_headers = {
                            k.lower(): v for k, v in response.headers.items() if k.lower() != "set-cookie"
                        }
                        if response.status_code in (301, 302, 303, 307, 308):
                            if hop == profile.max_redirects:
                                raise ValueError("source_redirect_limit")
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("source_redirect_without_location")
                            target = urljoin(str(response.url), location)
                            _check_ssrf(target)
                            redirects.append(
                                dict(
                                    url=str(response.url),
                                    status=response.status_code,
                                    location=location,
                                    resolved_target=target,
                                    headers=response_headers,
                                )
                            )
                            if origin(current) != origin(target):
                                request_headers = {
                                    k: v
                                    for k, v in request_headers.items()
                                    if k.lower() not in ("authorization", "cookie", "proxy-authorization")
                                }
                                client.cookies.clear()
                            current = target
                            continue
                        length = response.headers.get("content-length")
                        if length and int(length) > profile.max_bytes:
                            raise ByteLimitExceeded("source_declared_byte_limit")
                        chunks, size = [], 0
                        async for chunk in response.aiter_raw():
                            size += len(chunk)
                            if size > profile.max_bytes:
                                raise ByteLimitExceeded("source_streaming_byte_limit")
                            chunks.append(chunk)
                        return FetchedDocument(
                            url,
                            str(response.url),
                            redirects,
                            response.status_code,
                            response_headers,
                            b"".join(chunks),
                            timestamp(),
                        )
                raise ValueError("source_redirect_limit")
