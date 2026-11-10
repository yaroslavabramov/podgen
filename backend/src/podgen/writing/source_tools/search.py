"""Rank literal source-language hits, preserving offsets across whitespace and OCR line wraps."""

import re
from collections import Counter
from itertools import pairwise

PAGE_HITS = 6
# One preview line per nearby group of matches; read_source supplies the uncropped evidence.
NEIGHBORHOOD = 160


def _pattern(word):
    # Many scanned books hyphenate mid-word across physical lines.
    return r"(?:-\s*\n\s*)?".join(map(re.escape, word))


def occurrences(text, terms, spans):
    """Prefer exact phrases then compact groups of distinct terms; keep pagination deterministic."""
    expr = re.compile(r"\b(?:" + "|".join(_pattern(term) for term in terms) + r")\b", re.I)
    for start, end in spans:
        hits = [
            (m.start(), m.end(), re.sub(r"-\s*\n\s*", "", m[0]).casefold())
            for m in expr.finditer(text, start, end)
        ]
        right = 0
        counts = Counter()
        candidates = []
        for i, (pos, finish, token) in enumerate(hits):
            while right < len(hits) and hits[right][0] < pos + NEIGHBORHOOD:
                counts[hits[right][2]] += 1
                right += 1
            phrase = hits[i : i + len(terms)]
            exact = [v[2] for v in phrase] == terms and all(
                not re.search(r"\w", text[left[1] : following[0]]) for left, following in pairwise(phrase)
            )
            candidates.append(((int(exact), len(counts)), pos, finish))
            counts[token] -= 1
            if not counts[token]:
                del counts[token]
        # Avoid flooding a page with nearby words from the same passage.
        occupied = set()
        for score, pos, finish in sorted(candidates, key=lambda hit: (-hit[0][0], -hit[0][1], hit[1])):
            bucket = pos // NEIGHBORHOOD
            if bucket not in occupied:
                occupied.add(bucket)
                yield score, pos, finish


def search(documents, cards, args):
    if args.within is not None and args.within not in documents and args.within not in cards:
        return {"error": "unknown_source_or_material"}
    windows = {key: [(0, len(doc.text))] for key, doc in documents.items() if args.within in (None, key)}
    if args.within in cards:
        # The card locates sources; nonempty queries can search beyond the original excerpt.
        windows = {key: [(0, len(documents[key].text))] for key, _, _ in cards[args.within]}
    result = []
    if not args.query.strip() and args.within in cards:
        result = [(key, start, end) for key, start, end in cards[args.within]]
    elif not args.query.strip():
        rows = [
            dict(source_id=key, source=documents[key].title, length=len(documents[key].text))
            for key in windows
        ]
        return page(rows, args.offset, "sources")
    else:
        terms = re.findall(r"\w+", args.query.casefold())
        if not terms or len(terms) > 24 or len(args.query) > 300:
            return {"error": "supply_up_to_24_search_words_and_300_characters"}
        ranked = [
            (score, key, pos, end)
            for key, spans in windows.items()
            for score, pos, end in occurrences(documents[key].text, terms, spans)
        ]
        result = [
            (key, pos, end)
            for score, key, pos, end in sorted(ranked, key=lambda v: (-v[0][0], -v[0][1], v[1], v[2]))
        ]
    sliced = page(result, args.offset, "hits")
    sliced["hits"] = [
        dict(
            source_id=key,
            source=documents[key].title,
            start=start,
            end=end,
            read_start=max(0, start - 450),
            preview=documents[key].text[max(0, start - 50) : min(start + 200, end + 145)],
        )
        for key, start, end in sliced["hits"]
    ]
    return sliced


def page(rows, offset, key):
    next_offset = offset + PAGE_HITS
    return {key: rows[offset:next_offset], "next_offset": next_offset if next_offset < len(rows) else None}
