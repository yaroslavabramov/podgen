"""Explicit ephemeral adapter for offline qualification; never selected by run/preview/resume."""

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

    def candidates(self, query, vector, allowed, limit):
        rows = [row for row in self.rows if row[0].source_id in allowed]
        if not rows:
            return dict(dense=[], lexical=[], exact=[])
        dense = np.stack([row[3] for row in rows]) @ vector
        lexical, exact = rank(query, [row[1] for row in rows], [row[2] for row in rows], limit)
        return dict(
            dense=[
                (rows[i][0], float(dense[i]))
                for i in sorted(range(len(rows)), key=lambda i: (-float(dense[i]), i))[:limit]
            ],
            lexical=[(rows[i][0], score) for i, score in lexical],
            exact=[(rows[i][0], score) for i, score in exact],
        )
