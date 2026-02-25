"""Web content extraction with Jina Reader API and httpx fallback."""

import ipaddress
import logging
import socket
import re
from urllib.parse import quote, unquote, urlparse

import httpx

from app.config import settings
from app.ingestion.web_quality import contains_blocking_signal

logger = logging.getLogger(__name__)

_JINA_TITLE_RE = re.compile(r"^\s*Title:\s*(.+)$", re.IGNORECASE)
_WIKI_SECTION_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
_WIKI_TIMESTAMP_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")
_WIKIPEDIA_NAMESPACE_PREFIXES = {
    "category",
    "file",
    "help",
    "media",
    "mediawiki",
    "module",
    "portal",
    "special",
    "talk",
    "template",
    "template talk",
    "user",
    "user talk",
    "wikipedia",
    "wikipedia talk",
}

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
_MAX_REDIRECTS = 5
_DEFAULT_FETCH_HEADERS = {
    "User-Agent": settings.web_user_agent,
    "Accept-Language": settings.web_accept_language,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


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


def _origin(url: str) -> str:
    """Return scheme://host[:port] for origin comparison."""
    p = urlparse(url)
    port_suffix = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{p.scheme}://{p.hostname}{port_suffix}"


async def _safe_fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Fetch URL following redirects with SSRF validation on every hop.

    Sensitive headers (Authorization) are stripped when redirecting cross-origin.
    """
    current_headers = dict(_DEFAULT_FETCH_HEADERS)
    if headers:
        current_headers.update(headers)
    initial_origin = _origin(url)

    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=False,
    ) as client:
        current_url = url
        for _ in range(_MAX_REDIRECTS):
            response = await client.get(current_url, headers=current_headers)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect without Location header")
                # Resolve relative redirects
                current_url = str(response.next_request.url) if response.next_request else location
                _check_ssrf(current_url)
                # Drop auth headers on cross-origin redirect
                if _origin(current_url) != initial_origin:
                    current_headers.pop("Authorization", None)
                    current_headers.pop("authorization", None)
                continue
            response.raise_for_status()
            content_length = len(response.content)
            if content_length > _MAX_BYTES:
                raise ValueError(
                    f"Response too large: {content_length} bytes (limit: {_MAX_BYTES})"
                )
            return response
        raise ValueError(f"Too many redirects (>{_MAX_REDIRECTS}) for {url}")


async def extract_web(url: str) -> tuple[str, str | None, str | None]:
    """Extract content from a URL.

    Strategy:
      1. Try trafilatura (local, free, good boilerplate removal).
      2. Fall back to Jina Reader API (handles JS-heavy pages).
      3. Last resort: raw httpx fetch + basic HTML stripping.

    Returns:
        (text_content, title_or_none, published_date_str_or_none)

    Raises:
        ValueError: On SSRF violation or invalid URL.
        httpx.HTTPError: On network failures after all retries.
    """
    _check_ssrf(url)

    # --- 0. Wikipedia fast-path via official MediaWiki API ----------------------
    try:
        wiki_result = await _wikipedia_api_extract(url)
        if wiki_result:
            text, title, date = wiki_result
            if text.strip():
                logger.info("MediaWiki API succeeded for %s (%d chars)", url, len(text))
                return text, title, date
            logger.warning("MediaWiki API returned empty text for %s; falling back", url)
    except Exception:
        logger.warning("MediaWiki API failed for %s; falling back", url, exc_info=True)

    # --- 1. Try trafilatura (local, free, good quality) -------------------------
    try:
        result = await _trafilatura_extract(url)
        if result:
            text, title, date = result
            if text.strip() and not contains_blocking_signal(text):
                logger.info("Trafilatura succeeded for %s (%d chars)", url, len(text))
                return text, title, date
            elif contains_blocking_signal(text):
                logger.warning("Trafilatura returned blocked content for %s; trying Jina", url)
            else:
                logger.warning("Trafilatura returned empty for %s, trying Jina", url)
    except Exception:
        logger.warning("Trafilatura failed for %s, trying Jina", url, exc_info=True)

    # --- 2. Fallback: Jina Reader (handles JS-heavy pages) ---------------------
    if settings.jina_api_key:
        try:
            text, title = await _jina_extract(url)
            if text and text.strip():
                if contains_blocking_signal(text):
                    logger.warning(
                        "Jina Reader returned blocked/challenge content for %s; trying httpx fallback",
                        url,
                    )
                else:
                    logger.info("Jina Reader succeeded for %s (%d chars)", url, len(text))
                    return text, title, None
            else:
                logger.warning("Jina Reader returned empty for %s, falling back", url)
        except Exception:
            logger.warning("Jina Reader failed for %s, falling back to httpx", url, exc_info=True)

    # --- 3. Last resort: raw httpx + basic HTML strip --------------------------
    text, title = await _httpx_extract(url)
    logger.info("httpx fallback succeeded for %s (%d chars)", url, len(text))
    return text, title, None


async def _trafilatura_extract(url: str) -> tuple[str, str | None, str | None] | None:
    """Fetch HTML via httpx and extract content with trafilatura.

    Returns (text, title, date_str) or None if trafilatura couldn't extract content.
    The date_str is in "YYYY-MM-DD" format when available from HTML metadata.
    """
    import trafilatura

    response = await _safe_fetch(url)
    html = response.text

    text = trafilatura.extract(
        html,
        include_tables=True,
        include_comments=False,
        include_links=False,
    )

    if not text or not text.strip():
        return None

    # Extract title and date from HTML metadata
    title: str | None = None
    date: str | None = None
    meta = trafilatura.extract_metadata(html)
    if meta:
        if meta.title:
            title = meta.title
        if meta.date:
            date = meta.date

    return text, title, date


async def _jina_extract(url: str) -> tuple[str, str | None]:
    """Fetch content via Jina Reader API."""
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Authorization": f"Bearer {settings.jina_api_key}",
        "Accept": "text/markdown",
        "X-Return-Format": "markdown",
    }

    response = await _safe_fetch(jina_url, headers=headers)
    text = response.text

    title: str | None = None
    for line in text.splitlines()[:40]:
        match = _JINA_TITLE_RE.match(line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if candidate and candidate.lower() not in {"n/a", "na", "none", "null"}:
            title = candidate
            break

    return text, title


async def _httpx_extract(url: str) -> tuple[str, str | None]:
    """Fetch content directly via httpx with basic HTML-to-text conversion."""
    response = await _safe_fetch(url)
    content_type = response.headers.get("content-type", "")
    raw = response.text

    title: str | None = None

    if "text/html" in content_type:
        raw, title = _strip_html(raw)

    return raw, title


async def _wikipedia_api_extract(url: str) -> tuple[str, str | None, str | None] | None:
    """Extract clean article text from Wikipedia using MediaWiki API.

    Returns:
        (text, title, published_date_or_none), or None when URL is not a
        Wikipedia article URL or the API response is missing/invalid.
    """
    page = _parse_wikipedia_page(url)
    if page is None:
        return None

    host, title_slug = page
    api_url = (
        f"https://{host}/w/api.php"
        f"?action=query&format=json&formatversion=2"
        f"&prop=extracts|revisions&redirects=1"
        f"&explaintext=1&exsectionformat=wiki"
        f"&rvprop=timestamp&rvlimit=1&rvdir=newer"
        f"&titles={quote(title_slug, safe='')}"
    )
    response = await _safe_fetch(api_url)
    payload = response.json()

    pages = payload.get("query", {}).get("pages", [])
    if not pages:
        return None

    page_obj = pages[0]
    if page_obj.get("missing"):
        return None

    extract = str(page_obj.get("extract") or "").strip()
    if not extract:
        return None

    text = _wikipedia_sections_to_markdown(extract)
    title = page_obj.get("title")
    date = _extract_wikipedia_revision_date(page_obj)
    return text, title, date


def _parse_wikipedia_page(url: str) -> tuple[str, str] | None:
    """Return (host, page_slug) for article URLs like /wiki/Foo_Bar."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "wikipedia.org" and not host.endswith(".wikipedia.org"):
        return None

    path = parsed.path or ""
    if not path.startswith("/wiki/"):
        return None

    slug = unquote(path[len("/wiki/"):].strip("/")).replace(" ", "_")
    if not slug:
        return None

    # Exclude known namespace pages (Category:, File:, Talk:, etc.),
    # but keep valid article slugs that contain ":" in the title.
    prefix, sep, _ = slug.partition(":")
    if sep and prefix.strip().lower().replace("_", " ") in _WIKIPEDIA_NAMESPACE_PREFIXES:
        return None

    return host, slug


def _wikipedia_sections_to_markdown(text: str) -> str:
    """Convert MediaWiki heading markers (== ... ==) to markdown for chunking."""
    def _replace(match: re.Match[str]) -> str:
        marker = match.group(1)
        heading = match.group(2).strip()
        if not heading:
            return ""
        level = len(marker)
        md_level = 2 if level == 2 else 3
        return f"{'#' * md_level} {heading}"

    return _WIKI_SECTION_RE.sub(_replace, text)


def _extract_wikipedia_revision_date(page_obj: dict) -> str | None:
    """Extract YYYY-MM-DD from the first revision timestamp, if present."""
    revisions = page_obj.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        return None

    first = revisions[0]
    if not isinstance(first, dict):
        return None

    timestamp = first.get("timestamp")
    if not isinstance(timestamp, str):
        return None

    match = _WIKI_TIMESTAMP_DATE_RE.match(timestamp)
    if not match:
        return None
    return match.group(1)


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
