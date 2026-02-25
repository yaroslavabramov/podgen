"""Web content extraction with Jina Reader API and httpx fallback."""

import hashlib
import ipaddress
import logging
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

from app.config import settings
from app.ingestion.pdf import extract_pdf_bytes
from app.ingestion.web_quality import contains_blocking_signal

logger = logging.getLogger(__name__)

_JINA_TITLE_RE = re.compile(r"^\s*Title:\s*(.+)$", re.IGNORECASE)
_JINA_PUBLISHED_TIME_RE = re.compile(r"^\s*Published Time:\s*(\d{4}-\d{2}-\d{2})\s*$", re.IGNORECASE)
_JINA_LINK_RE = re.compile(
    r"\[([^\]]{1,240})\]\((\S+?)(?:\s+\"[^\"]*\"|\s+'[^']*')?\)"
)
_WIKI_SECTION_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$", re.MULTILINE)
_WIKI_TIMESTAMP_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T")
_PDF_URL_HINT_RE = re.compile(r"\.pdf(?:$|[?#])|/downloadpdf/", re.IGNORECASE)
_JINA_NAV_MARKERS = (
    "###### topics",
    "###### countries",
    "###### series",
    "advanced search",
    "resources",
    "about us",
    "search for other papers",
    "download pdf",
)
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
_REPO_ROOT = Path(__file__).resolve().parents[3]


class DNSResolutionError(ValueError):
    """Transient DNS resolution failure during SSRF pre-check."""


def _check_ssrf(url: str) -> None:
    """Resolve the URL hostname and reject requests to private/reserved IPs."""
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Cannot parse hostname from URL: {url}")

    infos = _resolve_host_infos(hostname)

    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for net in _BLOCKED_NETWORKS:
            if ip in net:
                raise ValueError(
                    f"SSRF blocked: {hostname} resolves to private address {ip}"
                )


def _resolve_host_infos(hostname: str):
    """Resolve DNS with bounded retries for transient resolver failures."""
    retries = max(1, int(settings.web_dns_resolve_retries))
    retry_delay_sec = max(0, int(settings.web_dns_resolve_retry_delay_ms)) / 1000.0
    last_error: socket.gaierror | None = None

    for attempt in range(retries):
        try:
            return socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            last_error = exc
            if attempt < retries - 1 and retry_delay_sec > 0:
                time.sleep(retry_delay_sec)

    raise DNSResolutionError(f"DNS resolution failed for {hostname}: {last_error}")


def _origin(url: str) -> str:
    """Return scheme://host[:port] for origin comparison."""
    p = urlparse(url)
    port_suffix = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{p.scheme}://{p.hostname}{port_suffix}"


def _safe_filename_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip())
    token = token.strip("-_")
    return token or "unknown"


def _resolve_dump_dir(raw_dir: str) -> Path:
    path = Path(raw_dir).expanduser()
    if path.is_absolute():
        return path
    return _REPO_ROOT / path


def _dump_extractor_output(
    *,
    url: str,
    extractor: str,
    status: str,
    text: str | None = None,
    title: str | None = None,
    date: str | None = None,
    error: str | None = None,
) -> None:
    """Write raw extractor output to disk when debug dump dir is configured."""
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return

    try:
        path = _resolve_dump_dir(dump_dir)
        path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        filename = (
            f"{ts}_{_safe_filename_token(extractor)}_"
            f"{_safe_filename_token(status)}_{url_hash}.md"
        )

        lines = [
            f"extractor: {extractor}",
            f"status: {status}",
            f"url: {url}",
            f"title: {title or ''}",
            f"date: {date or ''}",
            f"text_chars: {len(text) if text else 0}",
        ]
        if error:
            lines.append(f"error: {error}")
        lines.extend(["", text or ""])

        (path / filename).write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        logger.warning(
            "Failed to write web extractor debug dump (extractor=%s, status=%s, url=%s)",
            extractor,
            status,
            url,
            exc_info=True,
        )


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


def _looks_like_pdf_url(url: str) -> bool:
    return bool(_PDF_URL_HINT_RE.search(url))


def _response_is_pdf(response: httpx.Response, *, requested_url: str) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    if "application/pdf" in content_type:
        return True
    if _looks_like_pdf_url(requested_url) or _looks_like_pdf_url(str(response.url)):
        if "application/octet-stream" in content_type:
            return True
    return response.content.startswith(b"%PDF-")


def _extract_jina_published_date(text: str) -> str | None:
    for line in text.splitlines()[:80]:
        match = _JINA_PUBLISHED_TIME_RE.match(line.strip())
        if match:
            return match.group(1)
    return None


def _find_pdf_candidate_url_from_markdown(text: str, *, base_url: str) -> str | None:
    base_host = (urlparse(base_url).hostname or "").lower()
    if not base_host:
        return None

    for match in _JINA_LINK_RE.finditer(text[:220000]):
        label = match.group(1).strip().lower()
        href = match.group(2).strip()
        if not href:
            continue
        candidate = urljoin(base_url, href)
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host != base_host:
            continue
        if _looks_like_pdf_url(candidate):
            return candidate
        if "download pdf" in label or label in {"pdf", "full pdf"}:
            return candidate
    return None


def _is_navigation_heavy_jina_text(text: str) -> bool:
    if not text:
        return False

    sample = text[:220000]
    lower = sample.lower()
    marker_hits = sum(1 for marker in _JINA_NAV_MARKERS if marker in lower)
    link_count = len(_JINA_LINK_RE.findall(sample))
    lines = sample.splitlines()
    short_link_lines = sum(
        1
        for line in lines
        if len(line.strip()) <= 170 and ("](" in line or line.strip().startswith("["))
    )

    if marker_hits >= 3 and link_count >= 35:
        return True
    if link_count >= 120 and short_link_lines >= 80:
        return True
    return False


def _title_from_pdf_url(url: str) -> str | None:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").split("/")[-1]
    if not slug:
        return None
    slug = re.sub(r"\.pdf$", "", unquote(slug), flags=re.IGNORECASE)
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug or None


async def _extract_pdf_from_url(url: str) -> tuple[str, str | None, str | None] | None:
    _check_ssrf(url)
    response = await _safe_fetch(
        url,
        headers={"Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1"},
    )
    if not _response_is_pdf(response, requested_url=url):
        return None
    text = await extract_pdf_bytes(response.content, source_ref=url)
    title = _title_from_pdf_url(str(response.url)) or _title_from_pdf_url(url)
    return text, title, None


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

    # --- 0. Direct PDF URL fast-path --------------------------------------------
    if _looks_like_pdf_url(url):
        try:
            pdf_result = await _extract_pdf_from_url(url)
            if pdf_result:
                text, title, date = pdf_result
                _dump_extractor_output(
                    url=url,
                    extractor="web_pdf_direct",
                    status="success",
                    text=text,
                    title=title,
                    date=date,
                )
                logger.info("Direct PDF extraction succeeded for %s (%d chars)", url, len(text))
                return text, title, date
        except Exception:
            _dump_extractor_output(
                url=url,
                extractor="web_pdf_direct",
                status="error",
                error="exception during direct pdf extraction",
            )
            logger.warning("Direct PDF extraction failed for %s; continuing cascade", url, exc_info=True)

    # --- 0. Wikipedia fast-path via official MediaWiki API ----------------------
    try:
        wiki_result = await _wikipedia_api_extract(url)
        if wiki_result:
            text, title, date = wiki_result
            if text.strip():
                _dump_extractor_output(
                    url=url,
                    extractor="mediawiki",
                    status="success",
                    text=text,
                    title=title,
                    date=date,
                )
                logger.info("MediaWiki API succeeded for %s (%d chars)", url, len(text))
                return text, title, date
            _dump_extractor_output(
                url=url,
                extractor="mediawiki",
                status="empty",
                text=text,
                title=title,
                date=date,
            )
            logger.warning("MediaWiki API returned empty text for %s; falling back", url)
    except Exception:
        _dump_extractor_output(
            url=url,
            extractor="mediawiki",
            status="error",
            error="exception during mediawiki extraction",
        )
        logger.warning("MediaWiki API failed for %s; falling back", url, exc_info=True)

    # --- 1. Try trafilatura (local, free, good quality) -------------------------
    try:
        result = await _trafilatura_extract(url)
        if result:
            text, title, date = result
            blocked = contains_blocking_signal(text)
            if text.strip() and not blocked:
                _dump_extractor_output(
                    url=url,
                    extractor="trafilatura",
                    status="success",
                    text=text,
                    title=title,
                    date=date,
                )
                logger.info("Trafilatura succeeded for %s (%d chars)", url, len(text))
                return text, title, date
            elif blocked:
                _dump_extractor_output(
                    url=url,
                    extractor="trafilatura",
                    status="blocked",
                    text=text,
                    title=title,
                    date=date,
                )
                logger.warning("Trafilatura returned blocked content for %s; trying Jina", url)
            else:
                _dump_extractor_output(
                    url=url,
                    extractor="trafilatura",
                    status="empty",
                    text=text,
                    title=title,
                    date=date,
                )
                logger.warning("Trafilatura returned empty for %s, trying Jina", url)
    except Exception:
        _dump_extractor_output(
            url=url,
            extractor="trafilatura",
            status="error",
            error="exception during trafilatura extraction",
        )
        logger.warning("Trafilatura failed for %s, trying Jina", url, exc_info=True)

    # --- 2. Fallback: Jina Reader (handles JS-heavy pages) ---------------------
    if settings.jina_api_key:
        try:
            text, title = await _jina_extract(url)
            if text and text.strip():
                blocked = contains_blocking_signal(text)
                jina_date = _extract_jina_published_date(text)
                if blocked:
                    _dump_extractor_output(
                        url=url,
                        extractor="jina",
                        status="blocked",
                        text=text,
                        title=title,
                    )
                    logger.warning(
                        "Jina Reader returned blocked/challenge content for %s; trying httpx fallback",
                        url,
                    )
                else:
                    pdf_candidate_url = _find_pdf_candidate_url_from_markdown(text, base_url=url)
                    if pdf_candidate_url and _is_navigation_heavy_jina_text(text):
                        try:
                            promoted = await _extract_pdf_from_url(pdf_candidate_url)
                            if promoted:
                                pdf_text, pdf_title, pdf_date = promoted
                                chosen_date = pdf_date or jina_date
                                _dump_extractor_output(
                                    url=pdf_candidate_url,
                                    extractor="web_pdf_promoted",
                                    status="success",
                                    text=pdf_text,
                                    title=pdf_title or title,
                                    date=chosen_date,
                                )
                                logger.info(
                                    "Promoted PDF extraction for %s via %s (%d chars)",
                                    url,
                                    pdf_candidate_url,
                                    len(pdf_text),
                                )
                                return pdf_text, (pdf_title or title), chosen_date
                        except Exception:
                            _dump_extractor_output(
                                url=pdf_candidate_url,
                                extractor="web_pdf_promoted",
                                status="error",
                                error="exception during promoted pdf extraction",
                            )
                            logger.warning(
                                "Promoted PDF extraction failed for %s via %s; keeping Jina text",
                                url,
                                pdf_candidate_url,
                                exc_info=True,
                            )
                    _dump_extractor_output(
                        url=url,
                        extractor="jina",
                        status="success",
                        text=text,
                        title=title,
                        date=jina_date,
                    )
                    logger.info("Jina Reader succeeded for %s (%d chars)", url, len(text))
                    return text, title, jina_date
            else:
                _dump_extractor_output(
                    url=url,
                    extractor="jina",
                    status="empty",
                    text=text,
                    title=title,
                )
                logger.warning("Jina Reader returned empty for %s, falling back", url)
        except Exception:
            _dump_extractor_output(
                url=url,
                extractor="jina",
                status="error",
                error="exception during jina extraction",
            )
            logger.warning("Jina Reader failed for %s, falling back to httpx", url, exc_info=True)

    # --- 3. Last resort: raw httpx + basic HTML strip --------------------------
    text, title = await _httpx_extract(url)
    _dump_extractor_output(
        url=url,
        extractor="httpx",
        status="success",
        text=text,
        title=title,
    )
    logger.info("httpx fallback succeeded for %s (%d chars)", url, len(text))
    return text, title, None


async def _trafilatura_extract(url: str) -> tuple[str, str | None, str | None] | None:
    """Fetch HTML via httpx and extract content with trafilatura.

    Returns (text, title, date_str) or None if trafilatura couldn't extract content.
    The date_str is in "YYYY-MM-DD" format when available from HTML metadata.
    """
    import trafilatura

    response = await _safe_fetch(url)
    if _response_is_pdf(response, requested_url=url):
        text = await extract_pdf_bytes(response.content, source_ref=url)
        return text, _title_from_pdf_url(str(response.url)) or _title_from_pdf_url(url), None

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
