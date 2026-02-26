"""Web-source quality helpers: gate checks, text cleanup, and title fallback."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlparse

_NBSP_CHARS_RE = re.compile(r"[\u00a0\u2007\u202f\u2060\ufeff]")
_INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_INLINE_LINK_RE = re.compile(r"\[([^\]]+)]\((?:https?://|/)[^)]+\)")
_EMPTY_INLINE_LINK_RE = re.compile(r"\[\s*]\((?:https?://|/)[^)]+\)", re.IGNORECASE)
_LINK_REF_DEF_RE = re.compile(r"^\[[^\]]+]:\s+\S+\s*$", re.IGNORECASE)
_DIVIDER_RE = re.compile(r"^[-=*_]{3,}$")
_SETEXT_UNDERLINE_RE = re.compile(r"^[-=]{3,}$")
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$")
_TITLE_LINE_RE = re.compile(r"^\s*title:\s*(.+)$", re.IGNORECASE)
_HAS_WORD_RE = re.compile(r"[^\W_]", re.UNICODE)
_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_MULTI_SPACE_RE = re.compile(r"[^\S\n]+")
_TRAILING_SPACE_RE = re.compile(r" +\n")
_TOO_MANY_NEWLINES_RE = re.compile(r"\n{3,}")
_JS_LINK_ONLY_RE = re.compile(r"^\[[^\]]*]\(\s*javascript:.*\)$", re.IGNORECASE)
_EMPTY_LINK_ONLY_RE = re.compile(r"^\[[^\]]*]\((?:https?://|/)[^)]+\)$", re.IGNORECASE)
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)*(?:\[[ xX]\]\s*)*")
_NAV_LINE_RE = re.compile(
    r"^(skip to(?::|\b)|home|menu|search|sign in|login|subscribe|cookie(s)?|privacy|terms|contact)\b",
    re.IGNORECASE,
)
_SERVICE_LINE_RE = re.compile(
    r"^(published time:\s*\d{4}-\d{2}-\d{2}|loading\s+\[[^\]]+].*|dismiss this warning)$",
    re.IGNORECASE,
)
_TITLE_NOISE_RE = re.compile(
    r"^(topics|countries|series|resources|about|search|menu|home|open external link|external links?)$",
    re.IGNORECASE,
)
_TITLE_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

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
    cleaned = _EMPTY_INLINE_LINK_RE.sub(" ", cleaned)
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
        if _EMPTY_LINK_ONLY_RE.match(stripped):
            continue
        if _DIVIDER_RE.match(stripped) and not _is_setext_underline(lines, idx):
            continue
        if _SERVICE_LINE_RE.match(stripped):
            continue

        nav_candidate = _normalize_nav_candidate(stripped)
        if _NAV_LINE_RE.match(nav_candidate) and len(nav_candidate) <= 80:
            continue
        if _looks_like_top_navigation_cluster_line(stripped, nav_candidate, idx, lines):
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
    return value[:500]


def _normalize_nav_candidate(line: str) -> str:
    candidate = _BULLET_PREFIX_RE.sub("", line).strip()
    candidate = re.sub(r"^[>\[\]()\s]+", "", candidate).strip()
    return candidate


def _looks_like_top_navigation_cluster_line(
    original_line: str,
    nav_candidate: str,
    idx: int,
    lines: list[str],
) -> bool:
    """Drop obvious top-of-page nav clusters without removing article headings."""
    if idx > 80:
        return False
    if _is_setext_title_line(lines, idx):
        return False
    if len(nav_candidate) < 12 or len(nav_candidate) > 180:
        return False
    if re.search(r"[.!?。！？]", nav_candidate):
        return False
    if nav_candidate.count("|") >= 2:
        return True

    tokens = _TITLE_TOKEN_RE.findall(nav_candidate)
    if len(tokens) < 6:
        return False

    short_tokens = sum(1 for token in tokens if len(token) <= 4)
    alpha_tokens = [token for token in tokens if any(ch.isalpha() for ch in token)]
    if not alpha_tokens:
        return False
    titlecase_ratio = _titlecase_ratio(alpha_tokens)
    camelcase_noise = _camelcase_token_count(tokens)
    has_bullets = original_line.lstrip().startswith(("*", "-", "+"))

    if has_bullets and short_tokens >= 4 and titlecase_ratio >= 0.6:
        return True
    if camelcase_noise >= 2 and short_tokens >= 3:
        return True
    return False


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


def _is_setext_title_line(lines: list[str], idx: int) -> bool:
    if idx + 1 >= len(lines):
        return False
    return _SETEXT_UNDERLINE_RE.match(lines[idx + 1].strip()) is not None


def _titlecase_ratio(tokens: list[str]) -> float:
    alpha_tokens = 0
    titlecase_like = 0
    for token in tokens:
        first_alpha = next((ch for ch in token if ch.isalpha()), "")
        if not first_alpha:
            continue
        alpha_tokens += 1
        if first_alpha.isupper():
            titlecase_like += 1
    return titlecase_like / max(1, alpha_tokens)


def _camelcase_token_count(tokens: list[str]) -> int:
    count = 0
    for token in tokens:
        if len(token) < 10:
            continue
        if not any(ch.islower() for ch in token) or not any(ch.isupper() for ch in token):
            continue
        inner_upper = sum(1 for ch in token[1:] if ch.isupper())
        if inner_upper >= 2:
            count += 1
    return count


def _normalize_whitespace(text: str) -> str:
    text = _NBSP_CHARS_RE.sub(" ", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _TRAILING_SPACE_RE.sub("\n", text)
    text = _TOO_MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip()
