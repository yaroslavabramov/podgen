"""Ported legacy ingestion cleaning helpers."""

import re

from .patterns import (
    _BULLET_PREFIX_RE,
    _DIVIDER_RE,
    _EMPTY_INLINE_LINK_RE,
    _EMPTY_LINK_ONLY_RE,
    _HAS_WORD_RE,
    _INLINE_IMAGE_RE,
    _INLINE_LINK_RE,
    _JS_LINK_ONLY_RE,
    _LINK_REF_DEF_RE,
    _MULTI_SPACE_RE,
    _NAV_LINE_RE,
    _NBSP_CHARS_RE,
    _SERVICE_LINE_RE,
    _SETEXT_UNDERLINE_RE,
    _TITLE_TOKEN_RE,
    _TOO_MANY_NEWLINES_RE,
    _TRAILING_SPACE_RE,
    _URL_ONLY_RE,
)


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
