"""Persistent full-corpus retrieval with bounded tokenization, inference and result pages."""

from collections import OrderedDict
from itertools import islice

from podgen.domain.canonical import digest

from . import QueryRejected
from .assets import cache_root
from .config import validate
from .hybrid import HybridSearch
from .lexical import words
from .onnx_models import models
from .preview import page


class StreamingSearch(HybridSearch):
    def __init__(self, documents, cards, profile, *, index, network=None):
        self.profile = validate(profile)
        self.documents, self.cards, self.index = documents, cards, index
        self._models = network
        self.results = OrderedDict()
        self.refresh()

    @property
    def models(self):
        if self._models is None:
            self._models = models(str(cache_root()))
        return self._models

    def refresh(self):
        from .streaming_build import refresh

        revision = digest([[key, doc.digest] for key, doc in sorted(self.documents.items())])
        if getattr(self, "revision", None) == revision:
            # A completed immutable membership needs fresh authorization, not
            # repeated language detection, projection lookup and rebinding.
            self.index.check_access()
            return
        refresh(self)
        self.revision = revision
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
