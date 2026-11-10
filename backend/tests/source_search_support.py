"""Explicit inference test double; PostgreSQL tests still execute the actual production index adapter."""

import re

import numpy as np


class TinyModels:
    def __init__(self):
        self.embedded = 0

    def offsets(self, text):
        return [(match.start(), match.end()) for match in re.finditer(r"\S+", text)]

    def embed(self, texts):
        self.embedded += len(texts)
        values = np.zeros((len(texts), 384), dtype=np.float32)
        values[:, 0] = 1
        return values

    def query(self, text):
        return self.embed([text])[0]

    def rerank(self, query, texts):
        return [0.5] * len(texts)
