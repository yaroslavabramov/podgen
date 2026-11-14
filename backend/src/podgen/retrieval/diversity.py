"""Group agreeing windows or explicitly displayed, tightly bounded variants.

Whitespace may differ; words, punctuation, numbers, negations and names may not.
Every matched selector survives. Visible textual variants preserve both contexts;
shared text establishes neither equivalence nor publication genealogy.
"""

import json
import re
from difflib import SequenceMatcher

from podgen.persistence.source_reading import read_sources


def selector(row):
    return dict(source_id=row["source_id"], start=row["start"], end=row["end"])


class Windows:
    def __init__(self, documents):
        self.documents, self.loaded = documents, {}

    def get(self, row):
        key = row["source_id"], row["start"], row["end"]
        if key not in self.loaded:
            doc = self.documents[key[0]]
            width = key[2] - key[1]
            margin = min(4096, width, max(0, (16384 - width) // 2))
            start, end = max(0, key[1] - margin), min(len(doc.text), key[2] + margin)
            tokens = [
                (m.group(), start + m.start(), start + m.end())
                for m in re.finditer(r"\S+", doc.text[start:end])
            ]
            first = next((i for i, t in enumerate(tokens) if t[2] > key[1]), len(tokens))
            last = next((i for i, t in enumerate(tokens) if t[1] >= key[2]), len(tokens))
            core = [t[0] for t in tokens[first:last]]
            shingles = {tuple(core[i : i + 7]) for i in range(max(0, len(core) - 6))}
            self.loaded[key] = tokens, first, last, core, shingles
        return self.loaded[key]

    def union(self, left, right):
        a, af, al, ac, ash = self.get(left)
        b, bf, bl, bc, bsh = self.get(right)
        if min(len(ac), len(bc)) < 20 or len(ash & bsh) < 0.65 * min(len(ash), len(bsh)):
            return None
        matcher = SequenceMatcher(None, ac, bc, autojunk=False)
        match = matcher.find_longest_match()
        exact_overlap = match.size >= 0.75 * min(len(ac), len(bc))
        if not exact_overlap and matcher.ratio() < 0.85:
            return None
        shift = af + match.a - bf - match.b
        start, end = min(af, bf + shift), max(al, bl + shift)
        within_bounds = start >= 0 and end <= len(a) and start - shift >= 0 and end - shift <= len(b)
        # The unmatched ends must agree too. A similar topic or one shared
        # quotation cannot conceal a changed number, attribution or negation.
        if (
            exact_overlap
            and within_bounds
            and [t[0] for t in a[start:end]] == [t[0] for t in b[start - shift : end - shift]]
        ):
            return dict(left, start=a[start][1], end=a[end - 1][2], read_start=max(0, a[start][1] - 200))
        if matcher.ratio() < 0.85:
            return None
        from .passage_alignment import union

        return union(left, right, a, af, al, b, bf, bl, self.documents)


def group(rows, documents):
    windows = Windows(documents)
    groups = []
    with read_sources([documents[source_id] for source_id in sorted({row["source_id"] for row in rows})]):
        for row in rows:
            aliases = {tuple(item[key] for key in ("source_id", "start", "end")) for item in row["aliases"]}
            for current in groups:
                combined = windows.union(current, row)
                if combined is not None:
                    variants = {
                        json.dumps(v, sort_keys=True, ensure_ascii=False): v
                        for v in (
                            *current.get("variants", ()),
                            *row.get("variants", ()),
                            *combined.get("variants", ()),
                        )
                    }
                    if len(json.dumps(list(variants.values()), ensure_ascii=False).encode()) > 1024:
                        continue
                    aliases.update(
                        tuple(item[key] for key in ("source_id", "start", "end"))
                        for item in current["aliases"]
                    )
                    current.update(combined)
                    if variants:
                        current["variants"] = list(variants.values())
                    current["aliases"] = [
                        dict(zip(("source_id", "start", "end"), item, strict=True))
                        for item in sorted(aliases)
                    ]
                    break
            else:
                groups.append(dict(row))
    for rank, row in enumerate(groups, 1):
        row["rank"] = rank
        row["support_relation"] = (
            "overlapping_text_variants"
            if row.get("variants")
            else "shared_original_text"
            if len({a["source_id"] for a in row["aliases"]}) > 1
            else "original_windows"
        )
    return groups
