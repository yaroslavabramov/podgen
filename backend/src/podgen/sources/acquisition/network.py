"""Legacy SSRF controls: validate DNS, then connect to that exact address."""

import ipaddress
import socket
import time
from urllib.parse import urlparse

import httpcore
import httpx


class DNSResolutionError(ValueError):
    pass


def _resolve_host_infos(hostname, *, retries=2, retry_delay_seconds=0.05):
    for attempt in range(retries):
        try:
            return socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror as error:
            if attempt + 1 == retries:
                raise DNSResolutionError(f"DNS resolution failed for {hostname}: {error}") from error
            time.sleep(retry_delay_seconds)


def _validated_public_ip_strings(hostname):
    addresses = []
    for _, _, _, _, sockaddr in _resolve_host_infos(hostname):
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise ValueError(f"SSRF blocked: {hostname} resolves to non-public address {ip}")
        if str(ip) not in addresses:
            addresses.append(str(ip))
    if not addresses:
        raise DNSResolutionError(f"DNS returned no addresses for {hostname}")
    return addresses


def _check_ssrf(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("source_url_requires_public_http_or_https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source_url_credentials_forbidden")
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        raise ValueError("source_url_invalid_port")
    _validated_public_ip_strings(parsed.hostname)


class _SSRFGuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, backend=None):
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last_error = None
        for address in _validated_public_ip_strings(str(host)):
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
        raise last_error or httpcore.ConnectError("No validated public address")

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("Unix sockets are disabled for source acquisition")

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False, retries=0)
        self._pool._network_backend = _SSRFGuardedNetworkBackend()
