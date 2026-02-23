"""Web content extraction with Jina Reader API and httpx fallback."""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Private / reserved IP ranges that must be blocked to prevent SSRF
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),       # link-local
    ipaddress.ip_network("::1/128"),               # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),              # IPv6 private
    ipaddress.ip_network("fe80::/10"),             # IPv6 link-local
]

_MAX_BYTES = settings.web_max_file_size_mb * 1024 * 1024  # 50 MB default
_TIMEOUT = settings.web_fetch_timeout                      # 30s default


def _check_ssrf(url: str) -> None:
    """Resolve the URL hostname and reject requests to private/reserved IPs."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Cannot parse hostname from URL: {url}")

    # Resolve DNS before connecting
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for {hostname}: {exc}")

    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                raise ValueError(
                    f"SSRF blocked: {hostname} resolves to private address {ip}"
                )


async def extract_web(url: str) -> tuple[str, str | None]:
    """Extract content from a URL.

    Strategy:
      1. Try Jina Reader API (produces clean markdown).
      2. Fall back to raw httpx fetch + basic HTML stripping.

    Returns:
        (text_content, title_or_none)

    Raises:
        ValueError: On SSRF violation or invalid URL.
        httpx.HTTPError: On network failures after all retries.
    """
    _check_ssrf(url)

    # --- Try Jina Reader first -------------------------------------------------
    if settings.jina_api_key:
        try:
            text, title = await _jina_extract(url)
            if text and text.strip():
                logger.info("Jina Reader succeeded for %s (%d chars)", url, len(text))
                return text, title
            logger.warning("Jina Reader returned empty for %s, falling back", url)
        except Exception:
            logger.warning("Jina Reader failed for %s, falling back to httpx", url, exc_info=True)

    # --- Fallback: raw httpx ---------------------------------------------------
    text, title = await _httpx_extract(url)
    logger.info("httpx fallback succeeded for %s (%d chars)", url, len(text))
    return text, title


async def _jina_extract(url: str) -> tuple[str, str | None]:
    """Fetch content via Jina Reader API."""
    jina_url = f"https://r.jina.ai/{url}"

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        headers = {
            "Authorization": f"Bearer {settings.jina_api_key}",
            "Accept": "text/markdown",
            "X-Return-Format": "markdown",
        }

        response = await client.get(jina_url, headers=headers)
        response.raise_for_status()

        content_length = len(response.content)
        if content_length > _MAX_BYTES:
            raise ValueError(
                f"Response too large: {content_length} bytes (limit: {_MAX_BYTES})"
            )

        text = response.text

    # Jina Reader sometimes includes a title line "Title: ..."
    title: str | None = None
    lines = text.split("\n", 3)
    if lines and lines[0].startswith("Title:"):
        title = lines[0][len("Title:"):].strip()

    return text, title


async def _httpx_extract(url: str) -> tuple[str, str | None]:
    """Fetch content directly via httpx with basic HTML-to-text conversion."""
    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

        content_length = len(response.content)
        if content_length > _MAX_BYTES:
            raise ValueError(
                f"Response too large: {content_length} bytes (limit: {_MAX_BYTES})"
            )

        content_type = response.headers.get("content-type", "")
        raw = response.text

    title: str | None = None

    if "text/html" in content_type:
        raw, title = _strip_html(raw)

    return raw, title


def _strip_html(html: str) -> tuple[str, str | None]:
    """Minimal HTML to text conversion using the stdlib html.parser."""
    import re
    from html.parser import HTMLParser

    title: str | None = None

    class _Extractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts: list[str] = []
            self._in_title = False
            self._skip_tags = {"script", "style", "nav", "footer", "header", "aside"}
            self._skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in self._skip_tags:
                self._skip_depth += 1
            if tag == "title":
                self._in_title = True

        def handle_endtag(self, tag):
            if tag in self._skip_tags and self._skip_depth > 0:
                self._skip_depth -= 1
            if tag == "title":
                self._in_title = False
            if tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
                self.parts.append("\n")

        def handle_data(self, data):
            nonlocal title
            if self._in_title:
                title = data.strip()
            if self._skip_depth == 0:
                self.parts.append(data)

    extractor = _Extractor()
    extractor.feed(html)
    text = "".join(extractor.parts)

    # Collapse excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip(), title
