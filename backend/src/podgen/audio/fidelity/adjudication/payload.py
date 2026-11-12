"""Compact clause projection; complete source and raw ASR stay in their persisted records."""

import re

from ..tokens import expected_tokens, tokens

VERSION = "compact-source-clause-review-packet/3"
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def parts(text, words, first, last):
    if first < last:
        start, end = words[first]["char_start"], words[last - 1]["char_end"]
    else:
        start = end = words[first]["char_start"] if first < len(words) else len(text)
    previous = [m.end() for m in SENTENCE_END.finditer(text, 0, start)]
    following = SENTENCE_END.search(text, end)
    before = previous[-1] if previous else 0
    after = following.end() if following else len(text)
    anchors = dict(
        before=text[words[max(0, first - 4)]["char_start"] : start] if first else None,
        after=text[end : words[min(len(words), last + 4) - 1]["char_end"]] if last < len(words) else None,
    )
    return text[start:end], dict(before=text[before:start], after=text[end:after]), anchors


def source_parts(source, first, last, stored_tokens=None):
    utterances = source["source"]["utterances"]
    offsets = []
    offset = 0
    for row in utterances:
        offsets.append(offset)
        offset += len(row["text"]) + 1
    words = [
        dict(
            t,
            char_start=t["char_start"] + offsets[t["utterance_index"]],
            char_end=t["char_end"] + offsets[t["utterance_index"]],
        )
        for t in (stored_tokens if stored_tokens is not None else expected_tokens(utterances))
    ]
    return parts("\n".join(row["text"] for row in utterances), words, first, last)


def case_payload(
    case,
    recognition,
    source,
    offset_frames,
    frames,
    rate,
    alias,
    *,
    source_tokens=None,
    recognition_tokens=None,
):
    expected, context, source_anchors = source_parts(source, *case["expected_token_range"], source_tokens)
    excerpt, asr_context, asr_anchors = parts(
        recognition["text"],
        recognition_tokens if recognition_tokens is not None else tokens(recognition["text"]),
        *case["recognized_token_range"],
    )
    value = dict(
        case_id=alias,
        kind=case["kind"],
        expected_words=expected,
        expected_context=context,
        asr_transcript_excerpt=excerpt,
        asr_context=asr_context,
        localization=dict(
            basis=case["audio_range"]["basis"],
            uncertainties=list(dict.fromkeys(i["kind"] for i in case.get("timing_uncertainties", []))),
        ),
    )
    if case.get("operation"):
        value["operation"] = case["operation"]
        if case["operation"] in ("insert", "delete"):
            value["expected_anchors"] = source_anchors
            value["asr_anchors"] = asr_anchors
    issue = case.get("issue") or {}
    if issue.get("kind") == "transcript_segment_text_mismatch":
        segment_indexes = list(dict.fromkeys(t["segment_index"] for t in issue["segment_tokens"]))
        value["asr_representation_conflict"] = dict(
            transcript=excerpt, segment_words=[recognition["segments"][i]["text"] for i in segment_indexes]
        )
    return value


def window_segments(recognition, offset_frames, frames, rate):
    start, end = offset_frames / rate, (offset_frames + frames) / rate
    return [
        dict(
            text=s["text"], start_seconds=round(s["start"] - start, 3), end_seconds=round(s["end"] - start, 3)
        )
        for s in recognition["segments"]
        if s["end"] >= start and s["start"] <= end
    ]
