"""Exhaustive scoped dense retrieval, lexical/exact candidates, RRF and cross-encoder reranking."""

from collections import OrderedDict, defaultdict

import numpy as np

from . import QueryRejected, RetrievalUnavailable
from .assets import cache_root
from .chunks import Chunk, spans
from .config import validate
from .lexical import language, words
from .onnx_models import models
from .preview import page


class HybridSearch:
    def __init__(self, documents, cards, profile, *, index, network=None):
        self.profile = validate(profile)
        self.documents, self.cards, self.index = documents, cards, index
        self.results = OrderedDict()
        if (
            len(documents) > profile["max_documents"]
            or sum(len(doc.text) for doc in documents.values()) > profile["max_characters"]
        ):
            raise RetrievalUnavailable("source_search_corpus_limit_exceeded")
        self.models = network if network is not None else models(str(cache_root()))
        self.chunks = set()
        for source_id, doc in documents.items():
            offsets = self.models.offsets(doc.text)
            ranges = list(spans(doc.text, offsets, profile["chunk_tokens"], profile["overlap_tokens"]))
            if len(self.chunks) + len(ranges) > profile["max_chunks"]:
                raise RetrievalUnavailable("source_search_corpus_limit_exceeded")
            self.chunks.update(Chunk(source_id, start, end) for start, end in ranges)
            code = language(doc.text[:16000])
            if not index.has_document(source_id, doc, ranges, code):
                vectors = self.models.embed([doc.text[start:end] for start, end in ranges])
                if (
                    vectors.shape != (len(ranges), 384)
                    or not np.isfinite(vectors).all()
                    or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4)
                ):
                    raise RetrievalUnavailable("source_search_embedding_shape_mismatch")
                index.add_document(source_id, doc, ranges, code, vectors)

    def _scope(self, within):
        if within is None:
            return set(self.documents)
        if within in self.documents:
            return {within}
        if within in self.cards:
            return {key for key, _, _ in self.cards[within]}
        raise QueryRejected("unknown_source_or_material")

    def _page(self, rows, offset, field):
        limit = self.profile["page_hits"]
        return {
            field: rows[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "total_hits": len(rows),
        }

    def search(self, args):
        self.index.check_access()
        allowed = self._scope(args.within)
        if not args.query.strip():
            if args.within in self.cards:
                rows = [
                    dict(source_id=key, start=start, end=end, read_start=start)
                    for key, start, end in self.cards[args.within]
                ]
                return page(rows, self.documents, "", args.offset, self.profile)
            return self._page(
                [
                    dict(
                        source_id=key,
                        title_preview=self.documents[key].title[:96],
                        length=len(self.documents[key].text),
                    )
                    for key in self.documents
                    if key in allowed
                ],
                args.offset,
                "sources",
            )
        if len(args.query) > 1000 or not words(args.query):
            raise QueryRejected("supply_a_search_question_up_to_1000_characters")
        key = (args.query, args.within)
        if key not in self.results:
            self.results[key] = self._rank(args.query, allowed)
            while len(self.results) > 32:
                self.results.popitem(last=False)
        return page(self.results[key], self.documents, args.query, args.offset, self.profile)

    def diagnostics(self, args):
        return dict(
            retrieval_profile=self.profile,
            query=args.query,
            within=args.within,
            ranked=self.results.get((args.query, args.within), []),
        )

    def _rank(self, query, allowed):
        branches = self.index.candidates(
            query, self.models.query(query), allowed, self.profile["candidate_pool"]
        )
        reciprocal = defaultdict(float)
        diagnostics = defaultdict(dict)
        branch_ranks = defaultdict(dict)
        for name, branch in branches.items():
            for position, (chunk, score) in enumerate(branch, 1):
                if chunk.source_id not in allowed or chunk not in self.chunks:
                    raise RetrievalUnavailable("source_search_index_selector_mismatch")
                reciprocal[chunk] += 1 / (60 + position)
                diagnostics[chunk][name] = float(score)
                branch_ranks[chunk][name] = position
        candidates = sorted(reciprocal, key=lambda chunk: (-reciprocal[chunk], chunk.source_id, chunk.start))
        fusion_ranks = {chunk: rank for rank, chunk in enumerate(candidates, 1)}
        texts = [self.documents[c.source_id].text[c.start : c.end] for c in candidates]
        relevance = self.models.rerank(query, texts)
        if len(relevance) != len(candidates) or not np.isfinite(relevance).all():
            raise RetrievalUnavailable("source_search_reranker_shape_mismatch")
        ranked = sorted(
            zip(candidates, texts, relevance, strict=True),
            key=lambda row: (-row[2], -reciprocal[row[0]], row[0].source_id, row[0].start),
        )
        rows = []
        for chunk, _text, score in ranked:
            scores = {key: round(value, 8) for key, value in diagnostics[chunk].items()}
            scores.update(reranker=round(float(score), 8), rrf=round(reciprocal[chunk], 8))
            rows.append(
                dict(
                    rank=len(rows) + 1,
                    source_id=chunk.source_id,
                    start=chunk.start,
                    end=chunk.end,
                    read_start=max(0, chunk.start - 200),
                    scores=scores,
                    branch_ranks=branch_ranks[chunk],
                    fusion_rank=fusion_ranks[chunk],
                )
            )
        return rows
