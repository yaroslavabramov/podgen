import gzip
import socket

import httpx
import pytest

from podgen.sources.acquisition import network
from podgen.sources.acquisition.contracts import AcquisitionProfile, FetchedDocument
from podgen.sources.acquisition.http import ByteLimitExceeded, HttpSourceFetcher, decoded_body


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.consumed, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        network, "_resolve_host_infos", lambda host: [(None, None, None, None, ("93.184.216.34", 0))]
    )


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "100.64.0.1",
    ],
)
def test_nonpublic_dns_is_rejected(monkeypatch, address):
    monkeypatch.setattr(network, "_resolve_host_infos", lambda host: [(None, None, None, None, (address, 0))])
    with pytest.raises(ValueError, match="SSRF blocked"):
        network._check_ssrf("https://example.com/document")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://user:secret@example.com/a",
        "https://example.com:0/",
    ],
)
def test_disallowed_url_shape(public_dns, url):
    with pytest.raises(ValueError):
        network._check_ssrf(url)


def test_dns_retries_keep_validated_addresses(monkeypatch):
    calls = []

    def resolve(*args):
        calls.append(args)
        if len(calls) == 1:
            raise socket.gaierror("transient")
        return [(None, None, None, None, ("93.184.216.34", 0))]

    monkeypatch.setattr(network.socket, "getaddrinfo", resolve)
    assert network._resolve_host_infos("example.com", retry_delay_seconds=0)[0][-1][0] == "93.184.216.34"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stream_limit_stops_before_remaining_chunks(public_dns):
    stream = Stream([b"1234", b"5678", b"must not be read"])
    fetcher = HttpSourceFetcher(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    )
    with pytest.raises(ByteLimitExceeded, match="streaming"):
        await fetcher.fetch("https://example.com/a", AcquisitionProfile(max_bytes=6))
    assert stream.consumed == 2 and stream.closed


@pytest.mark.asyncio
async def test_declared_limit_rejects_before_body(public_dns):
    stream = Stream([b"never read"])
    fetcher = HttpSourceFetcher(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-length": "100"}, stream=stream)
        )
    )
    with pytest.raises(ByteLimitExceeded, match="declared"):
        await fetcher.fetch("https://example.com/a", AcquisitionProfile(max_bytes=6))
    assert stream.consumed == 0 and stream.closed


@pytest.mark.asyncio
async def test_relative_redirect_trace_and_cross_origin_credentials(public_dns):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "/next"}, stream=Stream([]))
        if len(calls) == 2:
            return httpx.Response(307, headers={"location": "https://other.example/final"}, stream=Stream([]))
        return httpx.Response(
            403, headers={"content-type": "text/plain"}, stream=Stream([b"actual original"])
        )

    result = await HttpSourceFetcher(transport=httpx.MockTransport(respond)).fetch(
        "https://example.com/a",
        AcquisitionProfile(),
        headers={"aUtHoRiZaTiOn": "secret", "Cookie": "private"},
    )
    assert result.body == b"actual original" and result.status_code == 403
    assert len(result.redirect_chain) == 2
    assert result.resolved_url == "https://other.example/final"
    assert calls[1].headers["authorization"] == "secret"
    assert "authorization" not in calls[2].headers and "cookie" not in calls[2].headers


@pytest.mark.asyncio
async def test_redirect_private_destination_blocked_before_second_request(monkeypatch):
    monkeypatch.setattr(
        network,
        "_resolve_host_infos",
        lambda host: [(None, None, None, None, (host if host == "127.0.0.1" else "93.184.216.34", 0))],
    )
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}, stream=Stream([]))

    with pytest.raises(ValueError, match="SSRF blocked"):
        await HttpSourceFetcher(transport=httpx.MockTransport(respond)).fetch(
            "https://example.com/a", AcquisitionProfile()
        )
    assert len(calls) == 1


def test_saved_compressed_original_has_separate_decoded_limit():
    raw = gzip.compress(b"a" * 10000)
    document = FetchedDocument(
        "https://example.com",
        "https://example.com",
        [],
        200,
        {"content-encoding": "gzip"},
        raw,
        "2026-11-13T00:00:00Z",
    )
    with pytest.raises(ByteLimitExceeded, match="decoded"):
        decoded_body(document, 100)
    assert decoded_body(document, 20000) == b"a" * 10000
    assert document.body == raw
