"""Text alignment provides source context, never invented acoustic timing."""


def expected_range(opcodes, start, end, expected_count):
    selected = []
    boundaries = []
    for operation, i1, i2, j1, j2 in opcodes:
        if operation == "equal":
            left, right = max(start, j1), min(end, j2)
            if left < right:
                selected.append((i1 + left - j1, i1 + right - j1))
        elif (j1 < end and j2 > start) or (j1 == j2 and start <= j1 <= end):
            if i1 < i2:
                selected.append((i1, i2))
            else:
                boundaries.append(i1)
    if selected:
        return min(a for a, _ in selected), max(b for _, b in selected)
    if boundaries:
        point = boundaries[0]
        return max(0, point - 1), min(expected_count, point + 1)
    return 0, expected_count


def source_context(utterances, expected, start, end):
    selected = expected[start:end]
    anchor = expected[min(start, len(expected) - 1)] if expected else None
    first = selected[0] if selected else anchor
    last = selected[-1] if selected else anchor
    return dict(
        expected_token_range=[start, end],
        expected_tokens=selected,
        expected_context=utterances[
            max(0, first["utterance_index"] - 1) if first else 0 : min(
                len(utterances), last["utterance_index"] + 2
            )
            if last
            else len(utterances)
        ],
        script_selectors=[
            dict(
                utterance_id=t["utterance_id"],
                utterance_index=t["utterance_index"],
                char_start=t["char_start"],
                char_end=t["char_end"],
            )
            for t in selected
        ],
    )
