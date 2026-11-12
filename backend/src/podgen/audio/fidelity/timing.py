from difflib import SequenceMatcher

from .tokens import tokens


def map_timing(recognized, transcript_tokens, sample_rate, frames):
    timed = []
    issues = []
    previous = 0
    invalid = set()
    for i, segment in enumerate(recognized.segments):
        start, end = round(segment.start * sample_rate), round(segment.end * sample_rate)
        if start < previous or end > frames:
            invalid.add(i)
            issues.append(
                dict(kind="invalid_segment_timeline", segment_index=i, start_frame=start, end_frame=end)
            )
        if start == end:
            invalid.add(i)
            issues.append(
                dict(kind="zero_length_segment_timestamp", segment_index=i, start_frame=start, end_frame=end)
            )
        previous = end
        for token in tokens(segment.text, segment_index=i):
            timed.append(dict(token, index=len(timed), start_frame=start, end_frame=end))
    match = SequenceMatcher(
        a=[t["normalized"] for t in transcript_tokens], b=[t["normalized"] for t in timed], autojunk=False
    )
    mapping, text_alignment = {}, {}
    for tag, i1, i2, j1, j2 in match.get_opcodes():
        if tag == "equal":
            for left, right in zip(range(i1, i2), range(j1, j2), strict=True):
                token = timed[right]
                text_alignment.setdefault(token["segment_index"], []).append(left)
                if (
                    token["segment_index"] not in invalid
                    and 0 <= token["start_frame"] <= token["end_frame"] <= frames
                ):
                    mapping[left] = token
        else:
            issues.append(
                dict(
                    kind="transcript_segment_text_mismatch",
                    transcript_token_range=[i1, i2],
                    segment_token_range=[j1, j2],
                    transcript_tokens=transcript_tokens[i1:i2],
                    segment_tokens=timed[j1:j2],
                )
            )
    for issue in issues:
        indexes = text_alignment.get(issue.get("segment_index"), [])
        if indexes:
            issue["transcript_token_range"] = [min(indexes), max(indexes) + 1]
    if abs(recognized.duration - frames / sample_rate) > 0.25:
        issues.append(
            dict(
                kind="reported_duration_mismatch",
                reported_seconds=recognized.duration,
                input_seconds=frames / sample_rate,
            )
        )
    return mapping, issues


def clip_range(start, end, mapping, frames, margin):
    direct = [mapping[i] for i in range(start, end) if i in mapping]
    left = next((mapping[i] for i in range(start - 1, -1, -1) if i in mapping), None)
    right = next((mapping[i] for i in sorted(mapping) if i >= end), None)
    if direct and len(direct) == end - start:
        first, last = min(t["start_frame"] for t in direct), max(t["end_frame"] for t in direct)
        basis = "matched_segment_timestamps"
    else:
        first, last = (left["end_frame"] if left else 0), (right["start_frame"] if right else frames)
        basis = "neighbor_anchors_or_full_audio_edges"
    first, last = max(0, min(first, last) - margin), min(frames, max(first, last) + margin)
    if first >= last:
        first, last = 0, frames
    return dict(
        start_frame=first,
        end_frame=last,
        basis=basis,
        margin_frames=margin,
        boundary_precision="recognizer_hypothesis",
    )
