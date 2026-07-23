from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import re
import unicodedata
from typing import Any

from app.generation_v2.tools.evidence_window_refs import (
    WindowRefError,
    create_window_ref,
)


_QUOTE_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
    }
)


def select_receipt_evidence_range(
    *,
    text: str,
    receipt: dict[str, Any],
    source_ref: str,
    source_version_id: Any,
    quote: str,
    start_offset: Any,
    end_offset: Any,
    max_chars: int,
) -> dict[str, Any]:
    identity_error = _receipt_identity_error(
        text=text,
        receipt=receipt,
        source_ref=source_ref,
        source_version_id=source_version_id,
    )
    if identity_error:
        return identity_error
    receipt_start = int(receipt["start_offset"])
    receipt_end = int(receipt["end_offset"])
    selected_start = receipt_start
    selected_end = receipt_end
    if quote:
        selection = _normalized_quote_selection(
            text=text,
            quote=quote,
            receipt_start=receipt_start,
            receipt_end=receipt_end,
            source_version_id=source_version_id,
            max_chars=max_chars,
        )
        if selection.get("error"):
            return selection
        selected_start = int(selection["start_offset"])
        selected_end = int(selection["end_offset"])
    elif start_offset not in {None, ""} and end_offset not in {None, ""}:
        try:
            selected_start = int(start_offset)
            selected_end = int(end_offset)
        except (TypeError, ValueError):
            return {"error": "receipt_subrange_offsets_must_be_integers"}
        if selected_start < receipt_start or selected_end > receipt_end or selected_end <= selected_start:
            return {
                "error": "receipt_subrange_out_of_bounds",
                "receipt_range": {"start_offset": receipt_start, "end_offset": receipt_end},
                "next_action": "Use one of the returned absolute ranges inside this receipt.",
            }
    if selected_end - selected_start > max_chars:
        return {
            "error": "receipt_range_too_large_for_exact_evidence",
            "selected_chars": selected_end - selected_start,
            "max_chars": max_chars,
            "receipt_range": {"start_offset": receipt_start, "end_offset": receipt_end},
            "next_action": "Retry with a shorter exact quote or absolute offsets inside the receipt range.",
        }
    try:
        window_ref = create_window_ref(
            source_version_id=source_version_id,
            text=text,
            start_offset=selected_start,
            end_offset=selected_end,
        )
    except WindowRefError:
        return {"error": "invalid_source_read_receipt_range"}
    return {
        "window_ref": window_ref,
        "start_offset": selected_start,
        "end_offset": selected_end,
    }


def _receipt_identity_error(
    *,
    text: str,
    receipt: dict[str, Any],
    source_ref: str,
    source_version_id: Any,
) -> dict[str, Any] | None:
    content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        receipt_start = int(receipt.get("start_offset"))
        receipt_end = int(receipt.get("end_offset"))
        receipt_total = int(receipt.get("total_chars"))
    except (TypeError, ValueError):
        return {"error": "invalid_source_read_receipt_range"}
    if (
        str(receipt.get("source_ref") or "") != source_ref
        or str(receipt.get("source_version_id") or "") != str(source_version_id)
        or str(receipt.get("content_sha256") or "") != content_sha256
        or receipt_total != len(text)
        or receipt_start < 0
        or receipt_end <= receipt_start
        or receipt_end > len(text)
    ):
        return {
            "error": "source_read_receipt_identity_mismatch",
            "message": "The pinned source text no longer matches this durable read receipt.",
        }
    return None


def _normalized_quote_selection(
    *,
    text: str,
    quote: str,
    receipt_start: int,
    receipt_end: int,
    source_version_id: Any,
    max_chars: int,
) -> dict[str, Any]:
    receipt_text = text[receipt_start:receipt_end]
    normalized_text, source_indexes = _normalize_with_source_indexes(receipt_text)
    normalized_quote, _ = _normalize_with_source_indexes(quote)
    if not normalized_quote:
        return {"error": "exact_quote_is_empty_after_normalization"}
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        match = normalized_text.find(normalized_quote, start)
        if match < 0:
            break
        raw_start = receipt_start + source_indexes[match]
        raw_end = receipt_start + source_indexes[match + len(normalized_quote) - 1] + 1
        matches.append((raw_start, raw_end))
        start = match + 1
    if len(matches) == 1:
        return {"start_offset": matches[0][0], "end_offset": matches[0][1]}
    if len(matches) > 1:
        return {
            "error": "quote_ambiguous_in_source_read_receipt",
            "message": "The normalized quote occurs more than once; choose the intended candidate explicitly.",
            "candidates": _candidate_payloads(
                text=text,
                ranges=matches[:3],
                allowed_start=receipt_start,
                allowed_end=receipt_end,
                source_version_id=source_version_id,
                max_chars=max_chars,
                match_kind="normalized_exact",
            ),
            "omitted_candidate_count": max(0, len(matches) - 3),
            "next_action": (
                "Retry read_source_window with the same receipt_ref and one candidate's absolute "
                "start_offset/end_offset."
            ),
        }
    suggested = _rank_candidate_ranges(
        receipt_text,
        quote=quote,
        absolute_start=receipt_start,
        max_chars=max_chars,
    )
    return {
        "error": "quote_not_found_in_source_read_receipt",
        "receipt_range": {"start_offset": receipt_start, "end_offset": receipt_end},
        "candidates": _candidate_payloads(
            text=text,
            ranges=suggested,
            allowed_start=receipt_start,
            allowed_end=receipt_end,
            source_version_id=source_version_id,
            max_chars=max_chars,
            match_kind="suggested_passage",
        ),
        "next_action": (
            "Inspect the candidates. If one is intended, retry with the same receipt_ref and that candidate's "
            "absolute start_offset/end_offset; otherwise select another exact range from the delivered receipt."
        ),
    }


def _normalize_with_source_indexes(value: str) -> tuple[str, list[int]]:
    normalized_chars: list[str] = []
    source_indexes: list[int] = []
    pending_space_index: int | None = None
    for index, raw_char in enumerate(str(value or "")):
        # NFKD is deliberate here: composed and decomposed Unicode then share
        # the same indexed representation even when normalization crosses raw
        # character boundaries. Case is preserved because this is exact quote
        # selection, not fuzzy search.
        expanded = unicodedata.normalize("NFKD", raw_char).translate(_QUOTE_TRANSLATION)
        for char in expanded:
            if char.isspace() or unicodedata.category(char).startswith("C"):
                if normalized_chars and normalized_chars[-1] != " ":
                    pending_space_index = index
                continue
            if pending_space_index is not None:
                normalized_chars.append(" ")
                source_indexes.append(pending_space_index)
                pending_space_index = None
            normalized_chars.append(char)
            source_indexes.append(index)
    return "".join(normalized_chars), source_indexes


def _rank_candidate_ranges(
    receipt_text: str,
    *,
    quote: str,
    absolute_start: int,
    max_chars: int,
) -> list[tuple[int, int]]:
    normalized_quote, _ = _normalize_with_source_indexes(quote)
    normalized_quote = normalized_quote.casefold()
    quote_tokens = set(re.findall(r"\w+", normalized_quote))
    ranges: set[tuple[int, int]] = set()
    for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", receipt_text, flags=re.DOTALL):
        start, end = match.span()
        if end - start <= max_chars:
            ranges.add((absolute_start + start, absolute_start + end))
        else:
            for anchor in _anchor_offsets(receipt_text[start:end], quote_tokens):
                local_start, local_end = _bounded_range(
                    len(receipt_text),
                    start + anchor,
                    max_chars=min(max_chars, 1_600),
                )
                ranges.add((absolute_start + local_start, absolute_start + local_end))
    if not ranges:
        for anchor in _anchor_offsets(receipt_text, quote_tokens) or [0]:
            local_start, local_end = _bounded_range(
                len(receipt_text),
                anchor,
                max_chars=min(max_chars, 1_600),
            )
            ranges.add((absolute_start + local_start, absolute_start + local_end))
    ranked: list[tuple[float, tuple[int, int]]] = []
    for raw_start, raw_end in ranges:
        candidate = receipt_text[raw_start - absolute_start : raw_end - absolute_start]
        normalized_candidate, _ = _normalize_with_source_indexes(candidate)
        normalized_candidate = normalized_candidate.casefold()
        candidate_tokens = set(re.findall(r"\w+", normalized_candidate))
        overlap = len(quote_tokens & candidate_tokens) / max(1, len(quote_tokens))
        similarity = SequenceMatcher(None, normalized_quote, normalized_candidate[: max(1, len(normalized_quote) * 3)]).ratio()
        ranked.append((overlap * 3 + similarity, (raw_start, raw_end)))
    ranked.sort(key=lambda item: (-item[0], item[1][0]))
    selected: list[tuple[int, int]] = []
    for _score, candidate in ranked:
        if any(_range_overlap(candidate, existing) > 0.7 for existing in selected):
            continue
        selected.append(candidate)
        if len(selected) == 3:
            break
    return selected


def _anchor_offsets(text: str, quote_tokens: set[str]) -> list[int]:
    if not quote_tokens:
        return []
    offsets: list[int] = []
    for match in re.finditer(r"\w+", text, flags=re.UNICODE):
        token = unicodedata.normalize("NFKC", match.group(0)).casefold()
        if token in quote_tokens:
            offsets.append(match.start())
        if len(offsets) >= 12:
            break
    return offsets


def _bounded_range(total: int, anchor: int, *, max_chars: int) -> tuple[int, int]:
    start = max(0, anchor - max_chars // 3)
    end = min(total, start + max_chars)
    start = max(0, end - max_chars)
    return start, end


def _candidate_payloads(
    *,
    text: str,
    ranges: list[tuple[int, int]],
    allowed_start: int,
    allowed_end: int,
    source_version_id: Any,
    max_chars: int,
    match_kind: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    allowed_start = max(0, min(allowed_start, len(text)))
    allowed_end = max(allowed_start, min(allowed_end, len(text)))
    allowed_length = allowed_end - allowed_start
    for raw_start, raw_end in ranges[:3]:
        start = max(allowed_start, min(raw_start, allowed_end))
        end = max(start, min(raw_end, allowed_end))
        if end <= start:
            continue
        if end - start > max_chars:
            local_start, local_end = _bounded_range(
                allowed_length,
                start - allowed_start,
                max_chars=max_chars,
            )
            start = allowed_start + local_start
            end = allowed_start + local_end
        try:
            window_ref = create_window_ref(
                source_version_id=source_version_id,
                text=text,
                start_offset=start,
                end_offset=end,
            )
        except WindowRefError:
            continue
        payloads.append(
            {
                "candidate_window_ref": window_ref,
                "start_offset": start,
                "end_offset": end,
                "passage_text": text[start:end],
                "match_kind": match_kind,
            }
        )
    return payloads


def _range_overlap(left: tuple[int, int], right: tuple[int, int]) -> float:
    overlap = max(0, min(left[1], right[1]) - max(left[0], right[0]))
    return overlap / max(1, min(left[1] - left[0], right[1] - right[0]))


__all__ = ["select_receipt_evidence_range"]
