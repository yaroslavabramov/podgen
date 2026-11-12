"""Locate approved source boundaries; ASR is a timing hypothesis, not a text gate."""

from podgen.audio.assembly.pcm import read
from podgen.audio.fidelity.timing import map_timing
from podgen.audio.fidelity.tokens import expected_tokens, tokens
from podgen.domain.errors import ContractError

from .boundaries import gap_frame
from .timing_groups import collapsed_onsets


def distances(a, b):
    rows = [list(range(len(b) + 1))]
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(row[-1] + 1, rows[-1][j] + 1, rows[-1][j - 1] + (left != right)))
        rows.append(row)
    return rows


def unique_cuts(expected, actual, boundaries):
    """Consider every optimal alignment, including repeated sentence alternatives."""
    forward = distances(expected, actual)
    backward = distances(expected[::-1], actual[::-1])
    optimum, n, m = forward[-1][-1], len(expected), len(actual)
    result = []
    for i in boundaries:
        candidates = [j for j in range(m + 1) if forward[i][j] + backward[n - i][m - j] == optimum]
        if len(candidates) != 1:
            raise ContractError("local_replacement_ambiguous_target_boundary")
        result.append(candidates[0])
    return result


def locate(utterances, target_range, recognition, audio):
    params, frames, pcm = read(audio)
    _, width, rate = params
    source, actual = expected_tokens(utterances), tokens(recognition.text)

    def normalized(rows):
        return [row["normalized"] for row in rows]

    low, high = target_range
    if not 0 <= low < high <= len(source):
        raise ContractError("local_replacement_invalid_source_target_range")
    internal = [cut for cut in (low, high) if cut not in (0, len(source))]
    located = iter(unique_cuts(normalized(source), normalized(actual), internal))
    cuts = [0 if cut == 0 else len(actual) if cut == len(source) else next(located) for cut in (low, high)]
    if not 0 <= cuts[0] < cuts[1] <= len(actual):
        raise ContractError("local_replacement_target_has_no_recognized_audio")
    timed, timing_groups = collapsed_onsets(recognition)
    mapping, diagnostics = map_timing(timed, actual, rate, frames)
    groups_by_index = {group["group_index"]: group for group in timing_groups}
    for anchor in mapping.values():
        segment = timed.segments[anchor["segment_index"]]
        anchor["segment_text"] = segment.text
        anchor["timestamp_scope"] = "recognizer_segment_envelope"
        anchor["individual_word_timing"] = (
            "unavailable" if len(tokens(segment.text)) > 1 else "recognizer_hypothesis"
        )
        if anchor["segment_index"] in groups_by_index:
            anchor["timing_group"] = groups_by_index[anchor["segment_index"]]
            anchor["timestamp_scope"] = "collapsed_word_group_envelope"
    # Segment text can differ from the top-level transcript; never pick a repeated
    # token's timestamp from an arbitrary SequenceMatcher alignment.
    segmented = [token for segment in timed.segments for token in tokens(segment.text)]
    internal_cuts = [
        cut for source_cut, cut in zip((low, high), cuts, strict=True) if source_cut not in (0, len(source))
    ]
    relevant = sorted({j for cut in internal_cuts for j in (cut - 1, cut) if 0 <= j < len(actual)})
    unique_cuts(normalized(actual), normalized(segmented), relevant + [j + 1 for j in relevant])
    boundaries, proofs = [], []
    for source_cut, cut in zip((low, high), cuts, strict=True):
        if source_cut == 0:
            if cut != 0:
                raise ContractError("local_replacement_unexpected_leading_audio")
            boundaries.append(0)
            proofs.append(dict(basis="recording_start"))
        elif source_cut == len(source):
            if cut != len(actual):
                raise ContractError("local_replacement_unexpected_trailing_audio")
            boundaries.append(frames)
            proofs.append(dict(basis="recording_end"))
        else:
            if cut - 1 not in mapping or cut not in mapping:
                raise ContractError("local_replacement_boundary_word_timing_missing")
            left, right = mapping[cut - 1], mapping[cut]
            a, b = left["end_frame"], right["start_frame"]
            if not 0 <= a < b <= frames:
                raise ContractError("local_replacement_boundary_words_overlap_or_no_gap")
            point, basis = gap_frame(pcm, width, rate, a, b, frames)
            boundaries.append(point)
            proofs.append(
                dict(basis=basis, gap_start_frame=a, gap_end_frame=b, left_word=left, right_word=right)
            )
    if boundaries[0] >= boundaries[1]:
        raise ContractError("local_replacement_nonmonotonic_extraction")
    return dict(
        start_frame=boundaries[0],
        end_frame=boundaries[1],
        source_target_token_range=target_range,
        recognized_target_token_range=cuts,
        boundary_proofs=proofs,
        recognition_diagnostics=diagnostics,
        collapsed_timing_groups=timing_groups,
        timing_precision="recognizer_hypothesis_not_acoustic_proof",
    )
