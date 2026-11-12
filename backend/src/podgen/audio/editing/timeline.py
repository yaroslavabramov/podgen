"""Source sentences and recognizer hypotheses with reversible script offsets."""

import re

from podgen.audio.fidelity.alignment import align, provenance
from podgen.audio.fidelity.timing import map_timing
from podgen.audio.fidelity.tokens import expected_tokens, tokens
from podgen.domain.errors import ContractError

from .boundaries import gap_frame


def sentences(utterances):
    result = []
    for index, utterance in enumerate(utterances):
        text = utterance["text"]
        starts = [0] + [match.end() for match in re.finditer(r'[.!?…][»”"\)]*\s+', text)]
        for first, last in zip(starts, starts[1:] + [len(text)], strict=True):
            while first < last and text[first].isspace():
                first += 1
            while last > first and text[last - 1].isspace():
                last -= 1
            if first < last:
                result.append(
                    dict(
                        utterance_index=index,
                        utterance_id=utterance["utterance_id"],
                        speaker=utterance["speaker"],
                        char_start=first,
                        char_end=last,
                        text=text[first:last],
                    )
                )
    return result


def build(utterances, recognition, pcm, width, rate, frames):
    source, actual = expected_tokens(utterances), tokens(recognition.text)
    mapping, diagnostics = map_timing(recognition, actual, rate, frames)
    opcodes, _ = align(source, actual)
    grouped = []
    for sentence in sentences(utterances):
        indices = [
            i
            for i, token in enumerate(source)
            if token["utterance_index"] == sentence["utterance_index"]
            and token["char_start"] >= sentence["char_start"]
            and token["char_end"] <= sentence["char_end"]
        ]
        if not indices:
            raise ContractError("local_replacement_sentence_without_words")
        low, high = min(indices), max(indices) + 1
        crosses = any(tag != "equal" and i1 < low < i2 for tag, i1, i2, _, _ in opcodes)
        if grouped and crosses:
            grouped[-1]["script_ranges"].append(sentence)
            grouped[-1]["high"] = high
        else:
            grouped.append(dict(script_ranges=[sentence], low=low, high=high))
    rows, pending = [], []
    for group in grouped:
        low, high = group["low"], group["high"]
        ranges = []
        for tag, i1, i2, j1, j2 in opcodes:
            if i1 >= high or i2 <= low:
                continue
            if tag == "equal":
                if i2 - i1 != j2 - j1:
                    raise ContractError("local_replacement_equal_alignment_not_one_to_one")
                ranges.append((j1 + max(i1, low) - i1, j1 + min(i2, high) - i1))
            else:
                ranges.append((j1, j2))
        anchors = [mapping[i] for a, b in ranges for i in range(a, b) if i in mapping]
        if not anchors:
            if rows:
                rows[-1]["script_ranges"].extend(group["script_ranges"])
                rows[-1]["timing_basis"] = "recognizer_group_with_unmapped_sentence"
            else:
                pending.extend(group["script_ranges"])
            continue
        row = dict(
            script_ranges=pending + group["script_ranges"],
            first_word_frame=min(t["start_frame"] for t in anchors),
            last_word_frame=max(t["end_frame"] for t in anchors),
            timing_basis="recognizer_hypothesis",
        )
        pending = []
        if rows and row["first_word_frame"] < rows[-1]["last_word_frame"]:
            rows[-1]["script_ranges"].extend(row["script_ranges"])
            rows[-1]["last_word_frame"] = max(rows[-1]["last_word_frame"], row["last_word_frame"])
            rows[-1]["timing_basis"] = "recognizer_group_with_overlapping_sentence_anchors"
        else:
            rows.append(row)
    if not rows:
        raise ContractError("local_replacement_script_has_no_audio_anchors")
    boundaries, bases = [0], ["recording_start"]
    for left, right in zip(rows, rows[1:], strict=False):
        point, basis = gap_frame(pcm, width, rate, left["last_word_frame"], right["first_word_frame"], frames)
        if point <= boundaries[-1]:
            raise ContractError("local_replacement_nonmonotonic_sentence_timing")
        boundaries.append(point)
        bases.append(basis)
    boundaries.append(frames)
    bases.append("recording_end")
    for i, row in enumerate(rows):
        row.update(
            start_frame=boundaries[i],
            end_frame=boundaries[i + 1],
            start_boundary_basis=bases[i],
            end_boundary_basis=bases[i + 1],
        )
    words = [
        dict(
            segment_index=i,
            text=segment.text,
            start_frame=round(segment.start * rate),
            end_frame=round(segment.end * rate),
            timing_basis="recognizer_hypothesis",
        )
        for i, segment in enumerate(recognition.segments)
    ]
    return dict(
        contract="audio-script-timeline/1",
        units=rows,
        word_segments=words,
        alignment=provenance(),
        recognition_diagnostics=diagnostics,
        timing_precision="recognizer_hypothesis_not_acoustic_proof",
    )


def replace(timeline, edits):
    units, words, invalidated = [], [], []
    for row in timeline["units"]:
        if any(
            row["start_frame"] < edit["end_frame"] and row["end_frame"] > edit["start_frame"]
            for edit in edits
        ):
            continue
        delta = sum(edit["delta_frames"] for edit in edits if edit["end_frame"] <= row["start_frame"])
        updated = dict(row, start_frame=row["start_frame"] + delta, end_frame=row["end_frame"] + delta)
        for field in ("first_word_frame", "last_word_frame"):
            if field in row:
                updated[field] = row[field] + delta
        units.append(updated)
    for row in timeline["word_segments"]:
        if any(
            row["start_frame"] < edit["end_frame"] and row["end_frame"] > edit["start_frame"]
            for edit in edits
        ):
            invalidated.append(row)
            continue
        delta = sum(edit["delta_frames"] for edit in edits if edit["end_frame"] <= row["start_frame"])
        words.append(dict(row, start_frame=row["start_frame"] + delta, end_frame=row["end_frame"] + delta))
    for edit in edits:
        units.append(
            dict(
                start_frame=edit["new_start_frame"],
                end_frame=edit["new_end_frame"],
                script_ranges=edit["script_ranges"],
                timing_basis="replacement_audio_group",
                internal_word_timing="unavailable_replace_whole_group_for_subsequent_repairs",
                start_boundary_basis="exact_splice",
                end_boundary_basis="exact_splice",
            )
        )
    return dict(
        timeline,
        units=sorted(units, key=lambda row: row["start_frame"]),
        word_segments=words,
        invalidated_parent_word_segments=invalidated,
        replacement_word_timing="unavailable_no_linear_interpolation",
    )
