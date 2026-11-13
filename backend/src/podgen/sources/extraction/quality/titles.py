"""Ported legacy ingestion titles helpers."""

import re
from urllib.parse import unquote, urlparse

from .cleaning import _normalize_nav_candidate
from .patterns import (
    _DIVIDER_RE,
    _EMPTY_LINK_ONLY_RE,
    _HAS_WORD_RE,
    _MARKDOWN_HEADER_RE,
    _NAV_LINE_RE,
    _SERVICE_LINE_RE,
    _SETEXT_UNDERLINE_RE,
    _TITLE_LINE_RE,
    _TITLE_NOISE_RE,
    _URL_ONLY_RE,
)


def resolve_web_title(
    extracted_title: str | None,
    *,
    text: str,
    url: str | None,
) -> str | None:
    """Pick the best available title from explicit title, content, or URL."""
    title, _meta = resolve_web_title_detailed(
        extracted_title,
        text=text,
        url=url,
    )
    return title


def resolve_web_title_detailed(
    extracted_title: str | None,
    *,
    text: str,
    url: str | None,
) -> tuple[str | None, dict[str, int | str]]:
    """Resolve title with scored candidates and include compact diagnostics."""
    raw_candidates = [
        ("extracted_title", extracted_title),
        ("title_line", _title_from_title_line(text)),
        ("setext_heading", _title_from_setext_heading(text)),
        ("markdown_heading", _title_from_markdown_heading(text)),
        ("first_meaningful", _title_from_first_meaningful_line(text)),
        ("url_slug", _title_from_url(url)),
    ]

    scored: list[tuple[int, str, str]] = []
    top_lines = [line.strip() for line in text.splitlines()[:120] if line.strip()]
    for source, raw in raw_candidates:
        normalized = _normalize_title(raw)
        if not normalized:
            continue
        score = _score_title_candidate(
            normalized,
            source=source,
            top_lines=top_lines,
            url=url,
        )
        scored.append((score, source, normalized))

    if not scored:
        return None, {"title_candidate_count": 0, "title_source": "none"}

    scored.sort(key=lambda item: (item[0], len(item[2])), reverse=True)
    best_score, best_source, best_title = scored[0]
    if best_score < 0:
        return None, {"title_candidate_count": len(scored), "title_source": "rejected"}
    return best_title, {
        "title_candidate_count": len(scored),
        "title_source": best_source,
        "title_score": best_score,
    }


def _title_from_title_line(text: str) -> str | None:
    for line in text.splitlines()[:40]:
        match = _TITLE_LINE_RE.match(line)
        if match:
            return match.group(1).strip()
    return None


def _title_from_markdown_heading(text: str) -> str | None:
    for line in text.splitlines()[:120]:
        match = _MARKDOWN_HEADER_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def _title_from_setext_heading(text: str) -> str | None:
    lines = text.splitlines()[:180]
    for idx in range(len(lines) - 1):
        current = lines[idx].strip()
        underline = lines[idx + 1].strip()
        if not current or not _SETEXT_UNDERLINE_RE.match(underline):
            continue
        if _NAV_LINE_RE.match(_normalize_nav_candidate(current)):
            continue
        if _SERVICE_LINE_RE.match(current):
            continue
        return current
    return None


def _title_from_first_meaningful_line(text: str) -> str | None:
    for line in text.splitlines()[:120]:
        stripped = line.strip()
        if not stripped:
            continue
        if _NAV_LINE_RE.match(_normalize_nav_candidate(stripped)):
            continue
        if _DIVIDER_RE.match(stripped):
            continue
        if _SERVICE_LINE_RE.match(stripped):
            continue
        if _EMPTY_LINK_ONLY_RE.match(stripped):
            continue
        if not _HAS_WORD_RE.search(stripped):
            continue
        if len(stripped) < 8 or len(stripped) > 180:
            continue
        if _URL_ONLY_RE.match(stripped):
            continue
        return stripped
    return None


def _title_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    slug = parsed.path.strip("/").split("/")[-1]
    if slug:
        slug = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", slug)
        slug = re.sub(r"[-_]+", " ", slug)
        slug = unquote(slug).strip()
        if slug:
            return slug
    return parsed.hostname


def _normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    value = title.strip().strip('"').strip("'")
    value = re.sub(r"^[#>\-\s]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return None
    if value.lower() in {"n/a", "na", "none", "null", "untitled"}:
        return None
    if len(value) < 3:
        return None
    return value


def _score_title_candidate(
    title: str,
    *,
    source: str,
    top_lines: list[str],
    url: str | None,
) -> int:
    source_score = {
        "extracted_title": 40,
        "title_line": 28,
        "setext_heading": 24,
        "markdown_heading": 18,
        "first_meaningful": 14,
        "url_slug": 2,
    }.get(source, 0)
    score = source_score

    lowered = title.lower().strip()
    if _TITLE_NOISE_RE.match(title):
        score -= 80
    if _NAV_LINE_RE.match(_normalize_nav_candidate(title)):
        score -= 40
    if _SERVICE_LINE_RE.match(title):
        score -= 40
    if len(title) < 8:
        score -= 15
    if len(title) > 180:
        score -= 12
    if not _HAS_WORD_RE.search(title):
        score -= 40

    if re.search(r"\d{4}", title):
        score += 4
    if re.search(r"[.!?。！？]", title):
        score += 2

    repeated = 0
    for line in top_lines:
        if line.lower() == lowered:
            repeated += 1
    if repeated >= 3:
        score -= 15
    elif repeated == 2:
        score -= 8

    if source != "url_slug" and url:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if lowered == host:
            score -= 25

    return score
