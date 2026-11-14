"""Explicit ephemeral adapter for offline qualification; never selected by run/preview/resume."""

from hashlib import sha256

import numpy as np

from .chunks import Chunk
from .lexical import rank


class MemoryIndex:
    def __init__(self):
        self.rows = []

    def check_access(self):
        pass

    def has_document(self, source_id, doc, ranges, language):
        return False

    def add_document(self, source_id, doc, ranges, language, vectors):
        self.rows.extend(
            (Chunk(source_id, start, end), doc.text[start:end], language, vector)
            for (start, end), vector in zip(ranges, vectors, strict=True)
        )

    def candidates(self, query, vector, allowed, limit, *, group_exact=False):
        rows = [row for row in self.rows if row[0].source_id in allowed]
        if not rows:
            return dict(dense=[], lexical=[], exact=[])
        dense = np.stack([row[3] for row in rows]) @ vector
        lexical, exact = rank(
            query, [row[1] for row in rows], [row[2] for row in rows], len(rows) if group_exact else limit
        )
        dense_order = sorted(range(len(rows)), key=lambda i: (-float(dense[i]), i))
        if group_exact:
            by_hash = {}
            for chunk, text, _, _ in rows:
                by_hash.setdefault(sha256(text.encode()).hexdigest(), []).append(chunk)

            def grouped(branch):
                selected = {}
                for i, score in branch:
                    key = sha256(rows[i][1].encode()).hexdigest()
                    selected.setdefault(key, score)
                ordered = sorted(selected, key=lambda key: (-selected[key], key))[:limit]
                return [
                    (
                        min(by_hash[key], key=lambda c: (c.source_id, c.start, c.end)),
                        float(selected[key]),
                        tuple(sorted(by_hash[key], key=lambda c: (c.source_id, c.start, c.end))),
                    )
                    for key in ordered
                ]

            return dict(
                dense=grouped([(i, float(dense[i])) for i in dense_order]),
                lexical=grouped(lexical),
                exact=grouped(exact),
            )
        return dict(
            dense=[(rows[i][0], float(dense[i])) for i in dense_order[:limit]],
            lexical=[(rows[i][0], score) for i, score in lexical],
            exact=[(rows[i][0], score) for i, score in exact],
        )
