"""Align close textual versions and expose every difference, without equivalence.

No semantic classification: numbers, names and negations receive the same exact
two-sided diff as other text. The caller admits only complete bounded diff payloads.
"""

from difflib import SequenceMatcher


def _project(opcodes, position, *, reverse=False, ending=False):
    for tag, a, b, c, d in opcodes:
        if reverse:
            a, b, c, d = c, d, a, b
        if a <= position <= b:
            return c + position - a if tag == "equal" else d if ending else c
    return None


def _version(source, tokens, start, end, documents):
    first = tokens[start][1] if start < len(tokens) else tokens[-1][2]
    last = tokens[end - 1][2] if end > start else first
    lo = tokens[max(0, start - 5)][1]
    hi = tokens[min(len(tokens) - 1, max(start, end - 1) + 5)][2]
    return dict(
        source_id=source,
        start=first,
        end=last,
        text=documents[source].text[first:last],
        context=dict(start=lo, end=hi, text=documents[source].text[lo:hi]),
    )


def union(left, right, a, af, al, b, bf, bl, documents):
    aa = [t[0] for t in a]
    bb = [t[0] for t in b]
    opcodes = SequenceMatcher(None, aa, bb, autojunk=False).get_opcodes()
    mapped = (
        _project(opcodes, bf, reverse=True),
        _project(opcodes, bl, reverse=True, ending=True),
        _project(opcodes, af),
        _project(opcodes, al, ending=True),
    )
    if any(value is None for value in mapped):
        return None
    start, end = min(af, mapped[0]), max(al, mapped[1])
    bs, be = min(bf, mapped[2]), max(bl, mapped[3])
    if end - start > al - af + bl - bf or be - bs > al - af + bl - bf:
        return None
    variants = []
    for tag, x, y, z, w in opcodes:
        if tag == "equal" or y <= start and w <= bs or x >= end and z >= be:
            continue
        # Never clip a change at a window boundary: uncertain alignments stay
        # separate rather than silently omitting the rest of an edit.
        if x < start or y > end or z < bs or w > be:
            return None
        variants.append(
            dict(
                versions=[
                    _version(left["source_id"], a, x, y, documents),
                    _version(right["source_id"], b, z, w, documents),
                ]
            )
        )
    if not variants:
        return None
    return dict(
        left, start=a[start][1], end=a[end - 1][2], read_start=max(0, a[start][1] - 200), variants=variants
    )
