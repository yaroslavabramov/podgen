"""Persistent full-corpus retrieval with bounded tokenization, inference and result pages."""

from collections import OrderedDict
from itertools import islice

import numpy as np

from podgen.domain.canonical import digest

from . import QueryRejected, RetrievalUnavailable
from .assets import cache_root
from .config import validate
from .hybrid import HybridSearch
from .lexical import language, words
from .onnx_models import models
from .preview import page
from .streaming_chunks import batches


class StreamingSearch(HybridSearch):
    def __init__(self, documents, cards, profile, *, index, network=None):
        self.profile = validate(profile)
        self.documents, self.cards, self.index = documents, cards, index
        self.models = network if network is not None else models(str(cache_root()))
        self.results = OrderedDict()
        self.refresh()

    def refresh(self):
        for source_id, doc in self.documents.items():
            code = language(doc.text[:16000])
            with self.index.document(source_id, doc, code) as state:
                if state["completed"]:
                    continue
                cursor = state["cursor_position"]
                for ranges, following in batches(doc.text, self.models, self.profile, cursor=cursor):
                    vectors = (
                        self.models.embed([doc.text[a:b] for a, b in ranges])
                        if ranges
                        else np.empty((0, 384))
                    )
                    if (
                        vectors.shape != (len(ranges), 384)
                        or not np.isfinite(vectors).all()
                        or (len(ranges) and not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4))
                    ):
                        raise RetrievalUnavailable("source_search_embedding_shape_mismatch")
                    self.index.append_batch(
                        source_id, doc, ranges, code, vectors, previous=cursor, cursor=following
                    )
                    cursor = following
                if not doc.text:
                    self.index.finish_empty(source_id)
        self.revision = digest([[key, doc.digest] for key, doc in sorted(self.documents.items())])
        self.results.clear()

    def _valid_chunk(self, chunk, allowed):
        return chunk.source_id in allowed and 0 <= chunk.start < chunk.end <= len(
            self.documents[chunk.source_id].text
        )

    def search(self, args):
        self.index.check_access()
        allowed = self._scope(args.within)
        if not args.query.strip():
            if args.within in self.cards:
                rows = [
                    dict(source_id=key, start=a, end=b, read_start=a) for key, a, b in self.cards[args.within]
                ]
                return page(rows, self.documents, "", args.offset, self.profile)
            selected = islice(
                (key for key in self.documents if key in allowed),
                args.offset,
                args.offset + self.profile["page_hits"],
            )
            sources = [
                dict(
                    source_id=key,
                    title_preview=self.documents[key].title[:96],
                    length=len(self.documents[key].text),
                )
                for key in selected
            ]
            following = args.offset + len(sources)
            return dict(
                sources=sources,
                total_hits=len(allowed),
                next_offset=following if following < len(allowed) else None,
            )
        if len(args.query) > 1000 or not words(args.query):
            raise QueryRejected("supply_a_search_question_up_to_1000_characters")
        request = dict(query=args.query, within=args.within, revision=self.revision)
        key = getattr(args, "result_version", None) or digest(request)
        if getattr(args, "result_version", None):
            rows = self.index.result_version(key, args.query, args.within)
        else:
            rows = self.index.result(key, request)
            if rows is None:
                rows = self.index.result(key, request, self._rank(args.query, allowed))
        self.results[(args.query, args.within)] = rows
        while len(self.results) > 32:
            self.results.popitem(last=False)
        return dict(
            page(rows, self.documents, args.query, args.offset, self.profile),
            result_version=key,
            result_scope="ranked_candidates",
            returned_candidates=len(rows),
        )
