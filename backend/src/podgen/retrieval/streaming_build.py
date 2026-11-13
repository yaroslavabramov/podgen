"""Bounded source preparation and resumable inference, with disjoint authorization phases."""

from itertools import islice

import numpy as np

from podgen.persistence.source_reading import read_sources

from . import RetrievalUnavailable
from .lexical import language
from .streaming_chunks import prepared_batches

PREPARE_BATCH = 64


def refresh(search):
    sources = iter(search.documents.items())
    while group := list(islice(sources, PREPARE_BATCH)):
        with read_sources([doc for _, doc in group]):
            documents = [(key, doc, language(doc.text[:16000])) for key, doc in group]
        prepare = getattr(search.index, "prepare_documents", None)
        ready = prepare(documents) if prepare else set()
        for source_id, doc, code in documents:
            if source_id not in ready:
                build(search, source_id, doc, code)


def build(search, source_id, doc, code):
    with search.index.document(source_id, doc, code) as state:
        if state["completed"]:
            return
        cursor = state["cursor_position"]
        prepared = iter(prepared_batches(doc.text, search.models, search.profile, cursor=cursor))
        while True:
            # Read/tokenize one bounded window and materialize one inference
            # batch. Close its read snapshot before any model computation.
            with read_sources([doc]):
                batch = next(prepared, None)
                if batch is None:
                    break
                ranges, following, texts = batch
            vectors = search.models.embed(texts) if ranges else np.empty((0, 384))
            if (
                vectors.shape != (len(ranges), 384)
                or not np.isfinite(vectors).all()
                or (len(ranges) and not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4))
            ):
                raise RetrievalUnavailable("source_search_embedding_shape_mismatch")
            search.index.append_batch(
                source_id, doc, ranges, code, vectors, previous=cursor, cursor=following, texts=texts
            )
            cursor = following
        if not doc.text:
            search.index.finish_empty(source_id)
