"""Legacy ingestion jina helpers, operating on saved content."""

import re
from urllib.parse import urljoin, urlparse

_JINA_TITLE_RE = re.compile(r"^\s*Title:\s*(.+)$", re.IGNORECASE)
_JINA_PUBLISHED_TIME_RE = re.compile(r"^\s*Published Time:\s*(\d{4}-\d{2}-\d{2})\s*$", re.IGNORECASE)
_JINA_LINK_RE = re.compile(r"\[([^\]]{1,240})\]\((\S+?)(?:\s+\"[^\"]*\"|\s+'[^']*')?\)")
_JINA_SETEXT_UNDERLINE_RE = re.compile(r"^\s*[=-]{3,}\s*$")
_JINA_ATX_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_JINA_EMPTY_LINK_LINE_RE = re.compile(r"^\s*\[[^\]]*]\((?:https?://|/)[^)]+\)\s*$", re.IGNORECASE)
_JINA_URL_ONLY_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)
_JINA_SERVICE_LINE_RE = re.compile(
    r"^\s*(published time:|loading \[|jump to content|dismiss this warning|skip to:)\s*",
    re.IGNORECASE,
)
_JINA_GENERIC_TITLE_RE = re.compile(
    r"^(open external link|topics|countries|series|resources|about|search|menu|home|external links?)$",
    re.IGNORECASE,
)
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
_JINA_TITLE_SCAN_LINES = 200


def _extract_jina_published_date(text: str) -> str | None:
    for line in text.splitlines()[:80]:
        match = _JINA_PUBLISHED_TIME_RE.match(line.strip())
        if match:
            return match.group(1)
    return None


def _normalize_jina_title_candidate(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    candidate = _JINA_LINK_RE.sub(r"\1", candidate)
    candidate = re.sub(r"^[#>\-\s]+", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" '\"")
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered in {"n/a", "na", "none", "null", "untitled"}:
        return None
    if _JINA_GENERIC_TITLE_RE.match(candidate):
        return None
    if _JINA_SERVICE_LINE_RE.match(candidate):
        return None
    if len(candidate) < 3 or len(candidate) > 220:
        return None
    if not any(ch.isalpha() or ch.isdigit() for ch in candidate):
        return None
    return candidate


def _extract_jina_title(text: str) -> str | None:
    if not text:
        return None

    lines = text.splitlines()[:_JINA_TITLE_SCAN_LINES]

    # 1) Explicit "Title: ..." metadata line (when present).
    for line in lines[:80]:
        match = _JINA_TITLE_RE.match(line)
        if not match:
            continue
        candidate = _normalize_jina_title_candidate(match.group(1))
        if candidate:
            return candidate

    # 2) Setext heading at top of markdown.
    for idx in range(len(lines) - 1):
        heading_line = lines[idx].strip()
        underline = lines[idx + 1].strip()
        if not heading_line or not _JINA_SETEXT_UNDERLINE_RE.match(underline):
            continue
        candidate = _normalize_jina_title_candidate(heading_line)
        if candidate:
            return candidate

    # 3) ATX markdown heading (# ...).
    for line in lines:
        match = _JINA_ATX_HEADING_RE.match(line)
        if not match:
            continue
        candidate = _normalize_jina_title_candidate(match.group(1))
        if candidate:
            return candidate

    # 4) First meaningful line fallback.
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Ignore raw metadata rows like "Title: ..."; only the normalized value
        # from step 1 can be used as a title candidate.
        if _JINA_TITLE_RE.match(stripped):
            continue
        if _JINA_EMPTY_LINK_LINE_RE.match(stripped):
            continue
        if _JINA_URL_ONLY_RE.match(stripped):
            continue
        if _JINA_SERVICE_LINE_RE.match(stripped):
            continue
        candidate = _normalize_jina_title_candidate(stripped)
        if candidate:
            return candidate
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
        1 for line in lines if len(line.strip()) <= 170 and ("](" in line or line.strip().startswith("["))
    )

    if marker_hits >= 3 and link_count >= 35:
        return True
    if link_count >= 120 and short_link_lines >= 80:
        return True
    return False


def _looks_like_pdf_url(url: str) -> bool:
    return bool(_PDF_URL_HINT_RE.search(url))
