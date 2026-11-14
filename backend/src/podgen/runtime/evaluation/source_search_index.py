"""Authorized project-scoped FTS/pgvector adapter; selectors refer only to an admitted frozen input."""

import json
from hashlib import sha256

from sqlalchemy.exc import SQLAlchemyError

from podgen.domain.canonical import digest
from podgen.persistence.database import one, query
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.config import storage_profile
from podgen.retrieval.lexical import words

from .source_search_access import IndexAccess

WHERE = "project_id=:p AND shared_input_record_id=:r AND profile_sha256=:h"


class PostgresSearchIndex:
    passage_table = "source_search_chunks"

    def __init__(self, journal, shared_input_ref, profile):
        self.journal, self.profile = journal, profile
        self.storage_profile = storage_profile(profile)
        self.access = IndexAccess(journal, shared_input_ref)
        self.params = dict(p=journal.project, r=shared_input_ref.record_id, h=digest(self.storage_profile))

    def check_access(self):
        try:
            with self.access.transaction():
                pass
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    def has_document(self, source_id, doc, ranges, language):
        self.access.source_hashes.add(doc.digest)
        try:
            with self.access.transaction() as (tx, hashes):
                row = one(
                    tx,
                    "SELECT *, (SELECT count(*) FROM source_search_chunks c WHERE c.project_id=i.project_id AND c.shared_input_record_id=i.shared_input_record_id AND c.profile_sha256=i.profile_sha256 AND c.source_id=i.source_id) AS actual_count FROM source_search_indexes i WHERE "
                    + WHERE
                    + " AND source_id=:s",
                    **self.params,
                    s=source_id,
                )
                if row is None:
                    return False
                if (
                    row["source_sha256"] != doc.digest
                    or row["source_length"] != len(doc.text)
                    or row["profile"] != self.storage_profile
                ):
                    raise RetrievalUnavailable("source_search_index_binding_mismatch")
                return (
                    row["spans"] == [list(pair) for pair in ranges]
                    and row["chunk_count"] == row["actual_count"] == len(ranges)
                    and row["language"] == language
                )
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    def add_document(self, source_id, doc, ranges, language, vectors):
        self.access.source_hashes.add(doc.digest)
        try:
            with self.access.transaction() as (tx, hashes):
                # A whole source projection is published atomically; failed inference never enters this transaction.
                query(
                    tx,
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                    key="source-search:"
                    + ":".join(str(value) for value in self.params.values())
                    + ":"
                    + source_id,
                )
                query(
                    tx,
                    "DELETE FROM source_search_indexes WHERE " + WHERE + " AND source_id=:s",
                    **self.params,
                    s=source_id,
                )
                query(
                    tx,
                    "INSERT INTO source_search_indexes VALUES(:p,:r,:h,:s,:sha,:length,CAST(:profile AS jsonb),CAST(:spans AS jsonb),:language,:count)",
                    **self.params,
                    s=source_id,
                    sha=doc.digest,
                    length=len(doc.text),
                    profile=json.dumps(self.storage_profile),
                    spans=json.dumps(ranges),
                    language=language,
                    count=len(ranges),
                )
                for fingerprint in sorted(hashes):
                    query(
                        tx,
                        "INSERT INTO source_search_dependencies VALUES(:p,:r,:h,:s,:sha)",
                        **self.params,
                        s=source_id,
                        sha=fingerprint,
                    )
                config = {"en": "english", "ru": "russian"}.get(language, "simple")
                for (start, end), vector in zip(ranges, vectors, strict=True):
                    text = doc.text[start:end]
                    tokens = words(text)
                    query(
                        tx,
                        "INSERT INTO source_search_chunks VALUES(:p,:r,:h,:s,:start,:end,:language,to_tsvector(CAST(:config AS regconfig),:text),:terms,:phrase,CAST(:vector AS vector),:passage)",
                        **self.params,
                        s=source_id,
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
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    def permitted(self):
        return (
            "WITH permitted AS MATERIALIZED (SELECT * FROM source_search_chunks WHERE "
            + WHERE
            + " AND source_id=ANY(:scope)) "
        )

    def prepare_passages(self, documents):
        from .passage_index import prepare

        prepare(self, documents)

    def candidates(self, text, vector, allowed, limit, *, group_exact=False):
        from .source_search_candidates import candidates

        return candidates(self, text, vector, allowed, limit, group_exact=group_exact)
