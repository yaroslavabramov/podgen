"""Content-addressed document computation, independent of runs and local source labels."""

import json
from contextlib import contextmanager
from hashlib import sha256

from podgen.domain.canonical import digest
from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.config import storage_profile
from podgen.retrieval.lexical import words

DOCUMENT = "project_id=:p AND document_key=:document"
VERSION = "source-document-projection/1"


def identity(doc, profile, language):
    # The explicit result policy changes ranking, never document computation.
    return digest(
        dict(
            version=VERSION,
            source_sha256=doc.digest,
            source_length=len(doc.text),
            profile=storage_profile(profile),
            language=language,
            coordinates="python-unicode-codepoints/1",
        )
    )


class DocumentProjection:
    def __init__(self, index):
        self.index = index

    def lock_key(self, key):
        return f"source-document:{self.index.journal.project}:{key}"

    def lock_transaction(self, tx, key):
        held = one(
            tx, "SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0)) AS held", key=self.lock_key(key)
        )
        if not held["held"]:
            raise RetrievalUnavailable("source_search_document_build_busy")

    @contextmanager
    def lock(self, key):
        # Inference holds a session lock, never a database transaction. Withdrawal
        # can commit while inference runs; the next authorized batch will fail.
        repo = self.index.journal.repo
        with repo.database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as tx:
            lock_key = self.lock_key(key)
            held = one(tx, "SELECT pg_try_advisory_lock(hashtextextended(:key,0)) AS held", key=lock_key)
            if not held["held"]:
                raise RetrievalUnavailable("source_search_document_build_busy")
            try:
                yield
            finally:
                query(tx, "SELECT pg_advisory_unlock(hashtextextended(:key,0))", key=lock_key)

    def begin(self, tx, key, doc, language):
        params = dict(p=self.index.journal.project, document=key)
        row = one(tx, "SELECT * FROM source_search_documents WHERE " + DOCUMENT, **params)
        if row is not None:
            if (row["source_sha256"], row["source_length"], row["profile"], row["language"]) != (
                doc.digest,
                len(doc.text),
                self.index.storage_profile,
                language,
            ):
                raise Conflict("source_search_document_binding_changed")
            return dict(row)
        query(
            tx,
            "INSERT INTO source_search_documents(project_id,document_key,source_sha256,source_length,profile_sha256,profile,language) "
            "VALUES(:p,:document,:sha,:length,:h,CAST(:profile AS jsonb),:language)",
            **params,
            sha=doc.digest,
            length=len(doc.text),
            h=self.index.params["h"],
            profile=json.dumps(self.index.storage_profile),
            language=language,
        )
        return dict(cursor_position=0, completed=False, chunk_count=0)

    def append(self, tx, key, doc, ranges, language, vectors, *, previous, cursor, texts):
        params = dict(p=self.index.journal.project, document=key)
        row = one(tx, "SELECT * FROM source_search_documents WHERE " + DOCUMENT + " FOR UPDATE", **params)
        if row is None or row["completed"] or row["cursor_position"] != previous:
            raise RetrievalUnavailable("source_search_build_cursor_changed")
        if not previous < cursor <= row["source_length"]:
            raise Conflict("source_search_build_cursor_invalid")
        config = {"en": "english", "ru": "russian"}.get(language, "simple")
        for (start, end), vector, text in zip(ranges, vectors, texts, strict=True):
            if not 0 <= start < end <= cursor:
                raise Conflict("source_search_chunk_range_invalid")
            if len(text) != end - start:
                raise Conflict("source_search_chunk_text_length_mismatch")
            tokens = words(text)
            query(
                tx,
                "INSERT INTO source_search_document_chunks VALUES(:p,:document,:start,:end,:language,"
                "to_tsvector(CAST(:config AS regconfig),:text),:terms,:phrase,CAST(:vector AS vector),:passage)",
                **params,
                start=start,
                end=end,
                language=language,
                config=config,
                text=text.casefold(),
                terms=sorted(set(tokens)),
                phrase=" ".join(tokens),
                vector=json.dumps(vector.tolist()),
                passage=sha256(text.encode()).hexdigest(),
            )
        query(
            tx,
            "UPDATE source_search_documents SET chunk_count=chunk_count+:count,cursor_position=:cursor,completed=:done WHERE "
            + DOCUMENT,
            **params,
            count=len(ranges),
            cursor=cursor,
            done=cursor == row["source_length"],
        )

    def finish_empty(self, tx, key):
        query(
            tx,
            "UPDATE source_search_documents SET completed=true WHERE "
            + DOCUMENT
            + " AND source_length=0 AND cursor_position=0",
            p=self.index.journal.project,
            document=key,
        )
