from __future__ import annotations

from collections.abc import Iterator
from collections import Counter
import heapq
import math
import re
from typing import Any

from app.generation_v2.tools.evidence_text_windows import (
    locator_type_for_label,
    nearest_locator_for_position,
    query_tokens,
    representative_text_windows,
    resolve_window_for_locator,
    source_outline,
)
from app.generation_v2.tools.evidence_utils import (
    _retrieval_channel_for_version,
    _uuid_or_none,
    _version_can_back_local_window,
)
from app.generation_v2.tools.evidence_window_refs import (
    create_window_ref,
    is_window_ref,
    parse_window_ref,
    resolve_window_ref,
)
from app.generation_v2.tools.evidence_source_text import best_readable_source_version
from app.generation_v2.tools.evidence_source_structure import (
    build_source_structure_page,
)
from app.models.generation_v2 import (
    EvidenceLocatorType,
    SourceRepresentationKind,
    SourceVersion,
    V2RecordStatus,
)
from app.models.source import Source, SourceType


_SOURCE_STRUCTURE_ITEM_LIMIT = 24
_SEARCH_SCORING_BLOCK_CHARS = 1_024
_SEARCH_SCORING_HALO_MAX_CHARS = 1_024
_SEARCH_SHORTLIST_MAX_BLOCKS = 64
_SEARCH_TERM_FREQUENCY_CAP = 64
_SEARCH_WORD_PATTERN = re.compile(r"[^\W_]{3,}", flags=re.UNICODE)


def _source_version_text(version: SourceVersion) -> str:
    from app.generation_v2.tools import evidence_retrieval

    return evidence_retrieval._source_version_text(version)


def _owner_source_version_text(owner: Any, version: SourceVersion) -> str:
    cached_getter = getattr(owner, "_cached_source_version_text", None)
    if callable(cached_getter):
        return str(cached_getter(version))
    return _source_version_text(version)


async def resolve_local_fallback_source_version(
    owner: Any, *, source_id: str, source_version_id: str
) -> SourceVersion | None:
    selected_version_ids = set(str(item) for item in owner.run_view.selected_source_version_ids)
    selected_source_ids = set(str(item) for item in owner.run_view.selected_source_ids)
    target_source_id = source_id
    if source_version_id and _uuid_or_none(source_version_id):
        import uuid

        version = await owner.db.get(SourceVersion, uuid.UUID(source_version_id))
        if (
            version is None
            or str(version.id) not in selected_version_ids
            or str(version.source_id) not in selected_source_ids
        ):
            return None
        if _version_can_back_local_window(version):
            return version
        target_source_id = str(version.source_id)
    if not target_source_id or not _uuid_or_none(target_source_id) or target_source_id not in selected_source_ids:
        return None
    import uuid

    from sqlalchemy import select

    from app.generation_v2.tools.evidence_utils import _uuid_list

    result = await owner.db.execute(
        select(SourceVersion).where(
            SourceVersion.source_id == uuid.UUID(target_source_id),
            SourceVersion.id.in_(_uuid_list(list(selected_version_ids))),
            SourceVersion.status == V2RecordStatus.READY,
        )
    )
    versions = [version for version in result.scalars() if _version_can_back_local_window(version)]
    return best_readable_source_version(
        versions,
        text_getter=lambda version: _owner_source_version_text(owner, version),
    )


async def read_source_window(
    owner: Any,
    version: SourceVersion,
    *,
    locator: str,
    max_chars: int,
) -> tuple[str, str, str, str]:
    text = _owner_source_version_text(owner, version)
    if text.strip():
        allow_timestamps = await source_allows_timestamp_locators(owner, version)
        if is_window_ref(locator):
            try:
                window = resolve_window_ref(locator, source_version_id=version.id, text=text)
            except ValueError:
                return "", locator, EvidenceLocatorType.UNKNOWN, _retrieval_channel_for_version(version)
            locator_text = (
                nearest_locator_for_position(
                    text,
                    window.start_offset,
                    allow_timestamps=allow_timestamps,
                )
                or "source window"
            )
            locator_type = locator_type_for_label(locator_text, allow_timestamps=allow_timestamps)
            if locator_text == "source window":
                locator_type = EvidenceLocatorType.SOURCE_LEVEL
            return (
                text[window.start_offset : window.end_offset],
                locator_text,
                locator_type,
                _retrieval_channel_for_version(version),
            )
        resolved = resolve_window_for_locator(
            text,
            locator=locator,
            max_chars=max_chars,
            allow_timestamps=allow_timestamps,
        )
        if resolved is not None:
            return (
                resolved.snippet_text,
                resolved.locator_text,
                resolved.locator_type,
                _retrieval_channel_for_version(version),
            )
    return "", "", EvidenceLocatorType.UNKNOWN, _retrieval_channel_for_version(version)


async def source_windows(owner: Any, version: SourceVersion, *, max_windows: int) -> list[dict[str, Any]]:
    text = _owner_source_version_text(owner, version)
    if not text.strip():
        return []
    allow_timestamps = await source_allows_timestamp_locators(owner, version)
    outline = source_outline(text, max_items=max_windows, allow_timestamps=allow_timestamps)
    structural_windows: list[dict[str, Any]] = []
    if outline:
        structural_limit = min(len(outline), max(1, max_windows // 2))
        for item in outline[:structural_limit]:
            resolved = resolve_window_for_locator(
                text,
                locator=item["locator"],
                max_chars=1600,
                allow_timestamps=allow_timestamps,
            )
            if resolved is not None:
                structural_windows.append(
                    {
                        "window_ref": create_window_ref(
                            source_version_id=version.id,
                            text=text,
                            start_offset=resolved.start_offset,
                            end_offset=resolved.end_offset,
                        ),
                        "locator_text": resolved.locator_text,
                        "locator_type": resolved.locator_type,
                        "snippet_text": resolved.snippet_text,
                        "channel": _retrieval_channel_for_version(version),
                    }
                )
    # Structure is only one orientation signal. A document with a single late heading
    # must not make the skim return only that section, so reserve most slots for broad
    # beginning-to-end coverage and merge the structural hints into it.
    representative_limit = max(1, max_windows - len(structural_windows))
    representative_windows: list[dict[str, Any]] = []
    for resolved in representative_text_windows(text, max_windows=representative_limit, max_chars=1600):
        representative_windows.append(
            {
                "window_ref": create_window_ref(
                    source_version_id=version.id,
                    text=text,
                    start_offset=resolved.start_offset,
                    end_offset=resolved.end_offset,
                ),
                "locator_text": resolved.locator_text,
                "locator_type": resolved.locator_type,
                "snippet_text": resolved.snippet_text,
                "channel": _retrieval_channel_for_version(version),
            }
        )
    combined = [*representative_windows, *structural_windows]
    combined.sort(key=lambda item: parse_window_ref(item["window_ref"]).start_offset)
    windows: list[dict[str, Any]] = []
    seen_ranges: set[tuple[int, int]] = set()
    for item in combined:
        parsed = parse_window_ref(item["window_ref"])
        range_key = (parsed.start_offset, parsed.end_offset)
        if range_key in seen_ranges:
            continue
        seen_ranges.add(range_key)
        windows.append(item)
    return windows[:max_windows]


async def sequential_source_window_page(
    owner: Any,
    version: SourceVersion,
    *,
    cursor: str,
    max_windows: int,
    max_chars: int = 1600,
) -> dict[str, Any]:
    """Return a contiguous, lossless page of stable source windows.

    Representative skims are useful for orientation but cannot prove that every part of a
    long unstructured source is reachable. This pager covers the raw text from offset zero
    through EOF without semantic truncation or gaps.
    """

    text = _owner_source_version_text(owner, version)
    if not text:
        return {
            "windows": [],
            "next_cursor": "",
            "total_chars": 0,
            "page_start": 0,
            "page_end": 0,
        }
    start = 0
    if cursor:
        try:
            parsed = resolve_window_ref(cursor, source_version_id=version.id, text=text)
        except ValueError as exc:
            raise ValueError("invalid_or_stale_source_window_cursor") from exc
        start = parsed.start_offset
    max_windows = max(1, max_windows)
    max_chars = max(200, max_chars)
    allow_timestamps = await source_allows_timestamp_locators(owner, version)
    windows: list[dict[str, Any]] = []
    position = start
    for _ in range(max_windows):
        if position >= len(text):
            break
        end = min(len(text), position + max_chars)
        nearest = nearest_locator_for_position(
            text,
            position,
            allow_timestamps=allow_timestamps,
        )
        locator_text = f"{nearest or 'source'} · chars {position + 1}-{end}"
        locator_type = (
            locator_type_for_label(nearest, allow_timestamps=allow_timestamps)
            if nearest
            else EvidenceLocatorType.SOURCE_LEVEL
        )
        windows.append(
            {
                "window_ref": create_window_ref(
                    source_version_id=version.id,
                    text=text,
                    start_offset=position,
                    end_offset=end,
                ),
                "locator_text": locator_text,
                "locator_type": locator_type,
                "snippet_text": text[position:end],
                "start_offset": position,
                "end_offset": end,
                "channel": _retrieval_channel_for_version(version),
            }
        )
        position = end
    next_cursor = ""
    if position < len(text):
        next_cursor = create_window_ref(
            source_version_id=version.id,
            text=text,
            start_offset=position,
            end_offset=min(len(text), position + max_chars),
        )
    return {
        "windows": windows,
        "next_cursor": next_cursor,
        "total_chars": len(text),
        "page_start": start,
        "page_end": position,
    }


async def read_source_text_page(
    owner: Any,
    version: SourceVersion,
    *,
    cursor: str,
    max_chars: int,
) -> dict[str, Any]:
    """Return one exact, contiguous source range without persisting evidence."""

    text = _owner_source_version_text(owner, version)
    if not text:
        return {
            "text": "",
            "start_offset": 0,
            "end_offset": 0,
            "total_chars": 0,
            "next_cursor": "",
            "eof": True,
        }
    start = 0
    if cursor:
        try:
            start = resolve_window_ref(
                cursor,
                source_version_id=version.id,
                text=text,
            ).start_offset
        except ValueError as exc:
            raise ValueError("invalid_or_stale_source_cursor") from exc
    if start >= len(text):
        raise ValueError("source_cursor_out_of_bounds")
    hard_end = min(len(text), start + max_chars)
    end = _natural_contiguous_end(text, start=start, hard_end=hard_end)
    next_cursor = ""
    if end < len(text):
        next_cursor = create_window_ref(
            source_version_id=version.id,
            text=text,
            start_offset=end,
            end_offset=min(len(text), end + max_chars),
        )
    result: dict[str, Any] = {
        "text": text[start:end],
        "start_offset": start,
        "end_offset": end,
        "total_chars": len(text),
        "window_ref": create_window_ref(
            source_version_id=version.id,
            text=text,
            start_offset=start,
            end_offset=end,
        ),
        "next_cursor": next_cursor,
        "eof": end == len(text),
    }
    if not cursor:
        structure = build_source_structure_page(
            text=text,
            source_version_id=version.id,
            allow_timestamps=await source_allows_timestamp_locators(owner, version),
            cursor="",
            max_items=_SOURCE_STRUCTURE_ITEM_LIMIT,
        )
        structure["structure_cursor"] = str(structure.pop("cursor", ""))
        structure["next_structure_cursor"] = str(
            structure.pop("next_cursor", "")
        )
        structure["next_action"] = (
            "If next_structure_cursor is non-empty, pass it only to inspect_source.cursor. "
            "Use the read_source result's top-level next_cursor to continue text."
        )
        result["structure"] = structure
    return result


def _natural_contiguous_end(text: str, *, start: int, hard_end: int) -> int:
    """Prefer a nearby completed text block while preserving exact contiguous coverage."""

    if hard_end >= len(text):
        return len(text)
    requested_chars = hard_end - start
    search_start = max(start + 1, hard_end - min(2_000, max(200, requested_chars // 4)))
    tail = text[search_start:hard_end]
    boundaries: list[int] = []
    for pattern in (r"\f+", r"\n[ \t]*\n+", r"(?m)(?=^#{1,6}\s+\S)"):
        boundaries.extend(search_start + match.end() for match in re.finditer(pattern, tail))
    return max(boundaries) if boundaries else hard_end


async def search_source_text_ranges(
    owner: Any,
    version: SourceVersion,
    *,
    query: str,
    max_results: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Return exact source ranges around lexical matches without creating evidence."""

    text = _owner_source_version_text(owner, version)
    if not text:
        return []
    allow_timestamps = await source_allows_timestamp_locators(owner, version)
    windows = _ranked_navigation_search_ranges(
        text,
        query=query,
        max_results=max_results,
        max_chars=max_chars,
        allow_timestamps=allow_timestamps,
    )
    results: list[dict[str, Any]] = []
    for window in windows:
        start = int(window["start_offset"])
        end = int(window["end_offset"])
        results.append(
            {
                "text": text[start:end],
                "start_offset": start,
                "end_offset": end,
                "total_chars": len(text),
                "window_ref": create_window_ref(
                    source_version_id=version.id,
                    text=text,
                    start_offset=start,
                    end_offset=end,
                ),
                "locator": str(window.get("locator_text") or "source range"),
                "locator_type": str(window.get("locator_type") or EvidenceLocatorType.UNKNOWN),
                "score": float(window.get("score") or 0.0),
                "boundary_kind": str(window.get("boundary_kind") or "exact_range"),
            }
        )
    return results


def _ranked_navigation_search_ranges(
    text: str,
    *,
    query: str,
    max_results: int,
    max_chars: int,
    allow_timestamps: bool,
) -> list[dict[str, Any]]:
    query_sequence = query_tokens(query)[:12]
    tokens = list(dict.fromkeys(query_sequence))
    if not tokens or max_results <= 0:
        return []
    token_set = set(tokens)
    halo_chars = min(
        _SEARCH_SCORING_HALO_MAX_CHARS,
        max(128, len(query.strip()) + 16, max(len(token) for token in tokens) + 16),
    )
    document_count = max(1, math.ceil(len(text) / _SEARCH_SCORING_BLOCK_CHARS))
    document_frequency = Counter[str]()
    total_block_tokens = 0
    for block in _iter_search_scoring_blocks(text, halo_chars=halo_chars):
        words = _scoring_block_words(text, block=block)
        core_words = [word for word, offset in words if block[0] <= offset < block[1]]
        total_block_tokens += max(1, len(core_words))
        document_frequency.update(set(core_words).intersection(token_set))
    token_idf = {
        token: math.log(
            1.0 + (document_count - document_frequency.get(token, 0) + 0.5) / (document_frequency.get(token, 0) + 0.5)
        )
        for token in tokens
    }
    average_block_tokens = max(1.0, total_block_tokens / document_count)
    literal_phrase_pattern = re.compile(re.escape(query.strip()), flags=re.IGNORECASE)
    normalized_phrase_pattern = re.compile(
        r"(?<!\w)" + r"[^\w_]+".join(re.escape(token) for token in query_sequence) + r"(?!\w)",
        flags=re.IGNORECASE,
    )
    shortlist: list[tuple[tuple[float | int, ...], dict[str, Any]]] = []
    for block in _iter_search_scoring_blocks(text, halo_chars=halo_chars):
        core_start, core_end, scoring_start, scoring_end = block
        words = _scoring_block_words(text, block=block)
        core_words = [word for word, offset in words if core_start <= offset < core_end]
        token_counts = Counter(word for word in core_words if word in token_set)
        covered_tokens = {token for token, count in token_counts.items() if count}
        if not covered_tokens:
            continue
        scoring_text = text[scoring_start:scoring_end]
        phrase_count, phrase_anchor = _bounded_pattern_match_stats(
            literal_phrase_pattern,
            scoring_text,
            text_offset=scoring_start,
            core_start=core_start,
            core_end=core_end,
        )
        normalized_phrase_count, normalized_phrase_anchor = _bounded_pattern_match_stats(
            normalized_phrase_pattern,
            scoring_text,
            text_offset=scoring_start,
            core_start=core_start,
            core_end=core_end,
        )
        full_sequence_count, full_sequence_anchor = _sequence_match_stats(
            words,
            sequence=tuple(query_sequence),
            core_start=core_start,
            core_end=core_end,
        )
        ngram_score, ngram_anchor = _query_ngram_score(
            words,
            query_sequence=query_sequence,
            token_idf=token_idf,
            core_start=core_start,
            core_end=core_end,
        )
        context_token_count = max(1, len(core_words))
        length_normalization = 1.0 - 0.75 + 0.75 * context_token_count / average_block_tokens
        bm25_score = sum(
            token_idf[token]
            * (min(count, _SEARCH_TERM_FREQUENCY_CAP) * 2.2)
            / (min(count, _SEARCH_TERM_FREQUENCY_CAP) + 1.2 * length_normalization)
            for token, count in token_counts.items()
            if count
        )
        weighted_coverage = sum(token_idf[token] for token in covered_tokens)
        rare_token_score = sum(token_idf[token] ** 2 for token in covered_tokens)
        rare_anchor = _rarest_token_anchor(
            words,
            covered_tokens=covered_tokens,
            token_idf=token_idf,
            core_start=core_start,
            core_end=core_end,
        )
        match_offset = next(
            (
                offset
                for offset in (
                    phrase_anchor,
                    normalized_phrase_anchor,
                    full_sequence_anchor,
                    rare_anchor,
                    ngram_anchor,
                )
                if offset is not None
            ),
            core_start,
        )
        score = float(
            phrase_count * 10_000
            + normalized_phrase_count * 5_000
            + full_sequence_count * 2_500
            + ngram_score * 200
            + bm25_score * 100
            + rare_token_score * 120
            + weighted_coverage * 25
        )
        quality: tuple[float | int, ...] = (
            int(bool(phrase_count)),
            int(bool(normalized_phrase_count or full_sequence_count)),
            score,
            ngram_score,
            rare_token_score,
            weighted_coverage,
            len(covered_tokens),
            -core_start,
        )
        candidate = {
            "match_offset": match_offset,
            "exact_phrase": bool(phrase_count),
            "normalized_phrase": bool(normalized_phrase_count or full_sequence_count),
            "score": score,
            "covered_token_count": len(covered_tokens),
            "weighted_coverage": weighted_coverage,
        }
        entry = (quality, candidate)
        if len(shortlist) < _SEARCH_SHORTLIST_MAX_BLOCKS:
            heapq.heappush(shortlist, entry)
        elif quality > shortlist[0][0]:
            heapq.heapreplace(shortlist, entry)

    ranked = [item for _quality, item in sorted(shortlist, key=lambda entry: entry[0], reverse=True)]
    selected: list[dict[str, Any]] = []
    for candidate in ranked:
        match_offset = int(candidate["match_offset"])
        start, end, boundary_kind = _bounded_completed_context_range(
            text,
            match_start=match_offset,
            max_chars=max_chars,
        )
        expanded = {
            **candidate,
            "start_offset": start,
            "end_offset": end,
            "boundary_kind": boundary_kind,
        }
        locator = (
            nearest_locator_for_position(
                text,
                match_offset,
                allow_timestamps=allow_timestamps,
            )
            or f"around query: {query}"
        )
        expanded["locator_text"] = locator
        expanded["locator_type"] = locator_type_for_label(
            locator,
            allow_timestamps=allow_timestamps,
        )
        if any(_ranges_substantially_overlap(expanded, existing) for existing in selected):
            continue
        selected.append(expanded)
        if len(selected) >= max_results:
            break
    return selected


def _iter_search_scoring_blocks(
    text: str,
    *,
    halo_chars: int,
) -> Iterator[tuple[int, int, int, int]]:
    for core_start in range(0, len(text), _SEARCH_SCORING_BLOCK_CHARS):
        core_end = min(len(text), core_start + _SEARCH_SCORING_BLOCK_CHARS)
        yield (
            core_start,
            core_end,
            max(0, core_start - halo_chars),
            min(len(text), core_end + halo_chars),
        )


def _scoring_block_words(
    text: str,
    *,
    block: tuple[int, int, int, int],
) -> list[tuple[str, int]]:
    _core_start, _core_end, scoring_start, scoring_end = block
    return [
        (match.group(0).lower(), scoring_start + match.start())
        for match in _SEARCH_WORD_PATTERN.finditer(text[scoring_start:scoring_end])
    ]


def _bounded_pattern_match_stats(
    pattern: re.Pattern[str],
    value: str,
    *,
    text_offset: int,
    core_start: int,
    core_end: int,
) -> tuple[int, int | None]:
    count = 0
    first_offset: int | None = None
    for match in pattern.finditer(value):
        offset = text_offset + match.start()
        if not (core_start <= offset < core_end):
            continue
        if first_offset is None:
            first_offset = offset
        count += 1
        if count >= _SEARCH_TERM_FREQUENCY_CAP:
            break
    return count, first_offset


def _sequence_match_stats(
    words: list[tuple[str, int]],
    *,
    sequence: tuple[str, ...],
    core_start: int,
    core_end: int,
) -> tuple[int, int | None]:
    if not sequence:
        return 0, None
    count = 0
    first_offset: int | None = None
    sequence_size = len(sequence)
    for index in range(0, len(words) - sequence_size + 1):
        offset = words[index][1]
        if not (core_start <= offset < core_end):
            continue
        if tuple(word for word, _offset in words[index : index + sequence_size]) != sequence:
            continue
        if first_offset is None:
            first_offset = offset
        count += 1
        if count >= _SEARCH_TERM_FREQUENCY_CAP:
            break
    return count, first_offset


def _query_ngram_score(
    words: list[tuple[str, int]],
    *,
    query_sequence: list[str],
    token_idf: dict[str, float],
    core_start: int,
    core_end: int,
) -> tuple[float, int | None]:
    best_anchor: int | None = None
    score = 0.0
    for size in range(2, min(4, len(query_sequence)) + 1):
        query_ngrams = {
            tuple(query_sequence[index : index + size]) for index in range(0, len(query_sequence) - size + 1)
        }
        matched: set[tuple[str, ...]] = set()
        for index in range(0, len(words) - size + 1):
            offset = words[index][1]
            if not (core_start <= offset < core_end):
                continue
            ngram = tuple(word for word, _offset in words[index : index + size])
            if ngram not in query_ngrams or ngram in matched:
                continue
            matched.add(ngram)
            if best_anchor is None or size == min(4, len(query_sequence)):
                best_anchor = offset
        score += sum(size * sum(token_idf[token] for token in ngram) for ngram in matched)
    return score, best_anchor


def _rarest_token_anchor(
    words: list[tuple[str, int]],
    *,
    covered_tokens: set[str],
    token_idf: dict[str, float],
    core_start: int,
    core_end: int,
) -> int | None:
    ranked_tokens = sorted(covered_tokens, key=lambda token: (-token_idf[token], token))
    for token in ranked_tokens:
        for word, offset in words:
            if word == token and core_start <= offset < core_end:
                return offset
    return None


def _bounded_completed_context_range(
    text: str,
    *,
    match_start: int,
    max_chars: int,
) -> tuple[int, int, str]:
    """Expand one shortlisted match using only bounded nearby text."""

    local_start = max(0, match_start - max_chars)
    local_end = min(len(text), match_start + max_chars)
    local_text = text[local_start:local_end]
    paragraph_spans = _paragraph_spans(local_text)
    local_match_start = match_start - local_start
    paragraph_index = next(
        (index for index, (start, end) in enumerate(paragraph_spans) if start <= local_match_start < end),
        None,
    )
    if paragraph_index is None:
        return _partial_context_range(text, match_start=match_start, max_chars=max_chars, kind="exact_range")
    range_start, range_end = paragraph_spans[paragraph_index]
    paragraph_is_complete = (range_start > 0 or local_start == 0) and (
        range_end < len(local_text) or local_end == len(text)
    )
    if not paragraph_is_complete or range_end - range_start > max_chars:
        return _partial_context_range(
            text,
            match_start=match_start,
            max_chars=max_chars,
            kind="partial_long_paragraph",
        )
    left = paragraph_index - 1
    right = paragraph_index + 1
    while True:
        options: list[tuple[int, str]] = []
        if left >= 0 and (paragraph_spans[left][0] > 0 or local_start == 0):
            options.append((range_end - paragraph_spans[left][0], "left"))
        if right < len(paragraph_spans) and (paragraph_spans[right][1] < len(local_text) or local_end == len(text)):
            options.append((paragraph_spans[right][1] - range_start, "right"))
        fitting = [option for option in options if option[0] <= max_chars]
        if not fitting:
            break
        _, direction = min(fitting, key=lambda item: item[0])
        if direction == "left":
            range_start = paragraph_spans[left][0]
            left -= 1
        else:
            range_end = paragraph_spans[right][1]
            right += 1
    return local_start + range_start, local_start + range_end, "complete_paragraphs"


def _partial_context_range(
    text: str,
    *,
    match_start: int,
    max_chars: int,
    kind: str,
) -> tuple[int, int, str]:
    start = max(0, match_start - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    return start, end, kind


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for separator in re.finditer(r"(?:\f+|\n[ \t]*\n+)", text):
        if separator.start() > start:
            spans.append((start, separator.start()))
        start = separator.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _ranges_substantially_overlap(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    overlap = max(
        0,
        min(int(left["end_offset"]), int(right["end_offset"]))
        - max(int(left["start_offset"]), int(right["start_offset"])),
    )
    if not overlap:
        return False
    shorter = min(
        int(left["end_offset"]) - int(left["start_offset"]),
        int(right["end_offset"]) - int(right["start_offset"]),
    )
    return overlap / max(1, shorter) >= 0.6


async def source_allows_timestamp_locators(owner: Any, version: SourceVersion) -> bool:
    if version.kind == SourceRepresentationKind.TRANSCRIPT:
        return True
    metadata = version.metadata_ or {}
    declared_type = str(
        metadata.get("source_type") or metadata.get("media_type") or metadata.get("asset_type") or ""
    ).lower()
    if declared_type in {SourceType.AUDIO, SourceType.VIDEO, "audio", "video", "transcript"}:
        return True
    try:
        source = await owner.db.get(Source, version.source_id)
    except (AttributeError, TypeError):
        source = None
    source_type = str(getattr(source, "source_type", "") or "").lower()
    return source_type in {SourceType.AUDIO, SourceType.VIDEO, "audio", "video"}


__all__ = [
    "_source_version_text",
    "read_source_window",
    "read_source_text_page",
    "resolve_local_fallback_source_version",
    "search_source_text_ranges",
    "sequential_source_window_page",
    "source_allows_timestamp_locators",
    "source_windows",
]
