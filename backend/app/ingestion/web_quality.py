"""Web-source quality helpers: gate checks, text cleanup, and title fallback."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlparse

_NBSP_CHARS_RE = re.compile(r"[\u00a0\u2007\u202f\u2060\ufeff]")
_INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)]\((?:https?://|/)[^)]+\)")
_LINK_REF_DEF_RE = re.compile(r"^\[[^\]]+]:\s+\S+\s*$", re.IGNORECASE)
_DIVIDER_RE = re.compile(r"^[-=*_]{3,}$")
_SETEXT_UNDERLINE_RE = re.compile(r"^[-=]{3,}$")
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$")
_TITLE_LINE_RE = re.compile(r"^\s*title:\s*(.+)$", re.IGNORECASE)
_HAS_WORD_RE = re.compile(r"[A-Za-zА-Яа-я0-9]")
_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r"[^\S\n]+")
_TRAILING_SPACE_RE = re.compile(r" +\n")
_TOO_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
_JS_LINK_ONLY_RE = re.compile(r"^\[[^\]]*]\(\s*javascript:.*\)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)*(?:\[[ xX]\]\s*)*")
_NAV_LINE_RE = re.compile(
    r"^(skip to (main )?content|home|menu|search|sign in|login|subscribe|cookie(s)?|privacy|terms|contact)\b",
    re.IGNORECASE,
)

_HTTP_403_ERROR_RE = re.compile(
    r"(target url returned error\s*403|returned error\s*403|status(?: code)?\s*[:=]?\s*403|error\s*[:=]?\s*403|^403\s+forbidden\b|403:\s*forbidden)",
    re.IGNORECASE,
)
_HTTP_ERROR_MAX_CHARS = 1200

_BLOCKED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("captcha_detected", re.compile(r"\bcaptcha\b", re.IGNORECASE)),
    ("verification_wall", re.compile(r"just a moment|security verification", re.IGNORECASE)),
    ("bot_check", re.compile(r"verify (that )?you are not a bot", re.IGNORECASE)),
    ("access_denied", re.compile(r"access denied|request blocked", re.IGNORECASE)),
    ("javascript_challenge", re.compile(r"enable javascript|cookies to continue", re.IGNORECASE)),
]


@dataclass(slots=True)
class WebQualityAssessment:
    state: str
    reasons: list[str]
    char_count: int
    non_whitespace_chars: int


def assess_web_fetch_health(text: str, *, min_chars: int) -> WebQualityAssessment:
    """Classify web extraction signal quality for Gate A."""
    normalized = _normalize_whitespace(text)
    blocked_reasons = _blocked_reasons(normalized)
    non_ws_chars = len(re.sub(r"\s+", "", text))
    raw_chars = len(text)

    if blocked_reasons:
        return WebQualityAssessment(
            state="blocked",
            reasons=blocked_reasons,
            char_count=len(normalized),
            non_whitespace_chars=non_ws_chars,
        )

    low_signal_reasons: list[str] = []
    if not normalized:
        low_signal_reasons.append("empty_text")
    if len(normalized) < min_chars:
        low_signal_reasons.append("too_short")
    if raw_chars >= 1000 and raw_chars > 0 and (non_ws_chars / raw_chars) < 0.2:
        low_signal_reasons.append("high_whitespace_ratio")

    state = "low_signal" if low_signal_reasons else "ok"
    return WebQualityAssessment(
        state=state,
        reasons=list(dict.fromkeys(low_signal_reasons)),
        char_count=len(normalized),
        non_whitespace_chars=non_ws_chars,
    )


def contains_blocking_signal(text: str) -> bool:
    normalized = _normalize_whitespace(text)
    return bool(_blocked_reasons(normalized))


def clean_web_text(text: str) -> str:
    """Remove common web/UI noise before chunking and metadata extraction."""
    if not text:
        return text

    cleaned = _INLINE_IMAGE_RE.sub(" ", text)
    cleaned = _INLINE_LINK_RE.sub(r"\1", cleaned)

    lines = cleaned.splitlines()
    filtered_lines: list[str] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            filtered_lines.append("")
            continue

        if _LINK_REF_DEF_RE.match(stripped):
            continue
        if _JS_LINK_ONLY_RE.match(stripped):
            continue
        if _DIVIDER_RE.match(stripped) and not _is_setext_underline(lines, idx):
            continue

        nav_candidate = _normalize_nav_candidate(stripped)
        if _NAV_LINE_RE.match(nav_candidate) and len(nav_candidate) <= 80:
            continue
        if _URL_ONLY_RE.match(stripped):
            continue

        filtered_lines.append(stripped)

    filtered_lines = _drop_repeated_short_lines(filtered_lines)
    filtered_lines = _drop_consecutive_duplicates(filtered_lines)
    return _normalize_whitespace("\n".join(filtered_lines))


def resolve_web_title(
    extracted_title: str | None,
    *,
    text: str,
    url: str | None,
) -> str | None:
    """Pick the best available title from explicit title, content, or URL."""
    for candidate in (
        extracted_title,
        _title_from_title_line(text),
        _title_from_markdown_heading(text),
        _title_from_first_meaningful_line(text),
        _title_from_url(url),
    ):
        normalized = _normalize_title(candidate)
        if normalized:
            return normalized
    return None


def _drop_repeated_short_lines(lines: list[str], *, threshold: int = 3, max_len: int = 100) -> list[str]:
    """Remove short lines that appear *threshold* or more times (boilerplate)."""
    counts: dict[str, int] = {}
    for line in lines:
        if not line:
            continue
        if len(line) <= max_len:
            counts[line] = counts.get(line, 0) + 1

    repeated = {line for line, count in counts.items() if count >= threshold}
    if not repeated:
        return lines
    return [line for line in lines if line not in repeated]


def _drop_consecutive_duplicates(lines: list[str]) -> list[str]:
    """Remove consecutive duplicate non-empty lines, keeping the first occurrence."""
    result: list[str] = []
    for line in lines:
        if line.strip() and result and line == result[-1]:
            continue
        result.append(line)
    return result



def _blocked_reasons(normalized_text: str) -> list[str]:
    normalized_lower = normalized_text.lower()
    reasons: list[str] = []
    for reason, pattern in _BLOCKED_PATTERNS:
        if pattern.search(normalized_lower):
            reasons.append(reason)

    # Handle HTTP 403 banners conservatively: short error pages only.
    if len(normalized_text) <= _HTTP_ERROR_MAX_CHARS and _HTTP_403_ERROR_RE.search(normalized_lower):
        reasons.append("http_403_error")

    return list(dict.fromkeys(reasons))


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


def _title_from_first_meaningful_line(text: str) -> str | None:
    for line in text.splitlines()[:120]:
        stripped = line.strip()
        if not stripped:
            continue
        if _NAV_LINE_RE.match(_normalize_nav_candidate(stripped)):
            continue
        if _DIVIDER_RE.match(stripped):
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
    return value[:500]


def _normalize_nav_candidate(line: str) -> str:
    candidate = _BULLET_PREFIX_RE.sub("", line).strip()
    candidate = re.sub(r"^[>\[\]()\s]+", "", candidate).strip()
    return candidate


def _is_setext_underline(lines: list[str], idx: int) -> bool:
    """Preserve markdown setext headings (Title + ===/---) for chunk structure."""
    current = lines[idx].strip()
    if not _SETEXT_UNDERLINE_RE.match(current):
        return False

    prev = _previous_non_empty_line(lines, idx - 1)
    if not prev:
        return False

    prev_stripped = prev.strip()
    if not prev_stripped:
        return False
    if len(prev_stripped) > 160:
        return False
    if _DIVIDER_RE.match(prev_stripped):
        return False
    if _URL_ONLY_RE.match(prev_stripped):
        return False
    if _JS_LINK_ONLY_RE.match(prev_stripped):
        return False
    if _NAV_LINE_RE.match(_normalize_nav_candidate(prev_stripped)):
        return False
    if not _HAS_WORD_RE.search(prev_stripped):
        return False

    # Adjacent divider clusters are usually decorative rules, not headings.
    nxt = _next_non_empty_line(lines, idx + 1)
    if nxt and _DIVIDER_RE.match(nxt.strip()):
        return False

    return True


def _previous_non_empty_line(lines: list[str], idx: int) -> str | None:
    while idx >= 0:
        line = lines[idx].strip()
        if line:
            return lines[idx]
        idx -= 1
    return None


def _next_non_empty_line(lines: list[str], idx: int) -> str | None:
    while idx < len(lines):
        line = lines[idx].strip()
        if line:
            return lines[idx]
        idx += 1
    return None


def _normalize_whitespace(text: str) -> str:
    text = _NBSP_CHARS_RE.sub(" ", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _TRAILING_SPACE_RE.sub("\n", text)
    text = _TOO_MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip()
