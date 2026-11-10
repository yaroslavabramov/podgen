"""Authorized project-scoped FTS/pgvector adapter; selectors refer only to an admitted frozen input."""

import json

from sqlalchemy.exc import SQLAlchemyError

from podgen.domain.canonical import digest
from podgen.persistence.database import one, query
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.chunks import Chunk
from podgen.retrieval.lexical import TOKEN, words

from .source_search_access import IndexAccess

WHERE = "project_id=:p AND shared_input_record_id=:r AND profile_sha256=:h"
CONFIG = "CASE language WHEN 'en' THEN 'english'::regconfig WHEN 'ru' THEN 'russian'::regconfig ELSE 'simple'::regconfig END"


class PostgresSearchIndex:
    def __init__(self, journal, shared_input_ref, profile):
        self.journal, self.profile = journal, profile
        self.access = IndexAccess(journal, shared_input_ref)
        self.params = dict(p=journal.project, r=shared_input_ref.record_id, h=digest(profile))

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
                    or row["profile"] != self.profile
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
                    profile=json.dumps(self.profile),
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
                        "INSERT INTO source_search_chunks VALUES(:p,:r,:h,:s,:start,:end,:language,to_tsvector(CAST(:config AS regconfig),:text),:terms,:phrase,CAST(:vector AS vector))",
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
                    )
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    def candidates(self, text, vector, allowed, limit):
        params = dict(
            self.params, scope=sorted(allowed), q=text, vector=json.dumps(vector.tolist()), limit=limit
        )
        permitted = (
            "WITH permitted AS MATERIALIZED (SELECT * FROM source_search_chunks WHERE "
            + WHERE
            + " AND source_id=ANY(:scope)) "
        )
        statements = dict(
            dense="SELECT source_id,start_offset,end_offset,1-(embedding <=> CAST(:vector AS vector)) AS score FROM permitted ORDER BY embedding <=> CAST(:vector AS vector),source_id,start_offset LIMIT :limit",
            lexical=f"SELECT source_id,start_offset,end_offset,ts_rank_cd(lexical,websearch_to_tsquery({CONFIG},:lexical_query)) AS score FROM permitted WHERE lexical @@ websearch_to_tsquery({CONFIG},:lexical_query) ORDER BY score DESC,source_id,start_offset LIMIT :limit",
            exact="SELECT source_id,start_offset,end_offset,((strpos(' '||phrase_text||' ',:phrase)>0)::int + (SELECT count(*)::float FROM unnest(CAST(:entities AS text[])) AS e(term) WHERE e.term=ANY(exact_terms))/GREATEST(cardinality(CAST(:entities AS text[])),1)) AS score FROM permitted WHERE strpos(' '||phrase_text||' ',:phrase)>0 OR exact_terms && CAST(:entities AS text[]) ORDER BY score DESC,source_id,start_offset LIMIT :limit",
        )
        params.update(
            lexical_query=" OR ".join('"' + word + '"' for word in words(text)),
            phrase=" " + " ".join(words(text)) + " ",
            entities=sorted(
                {
                    word.casefold()
                    for word in TOKEN.findall(text)
                    if any(c.isdigit() for c in word) or word[0].isupper()
                }
            ),
        )
        try:
            with self.access.transaction() as (tx, hashes):
                return {
                    name: [
                        (Chunk(row["source_id"], row["start_offset"], row["end_offset"]), float(row["score"]))
                        for row in query(tx, permitted + statement, **params).mappings()
                    ]
                    for name, statement in statements.items()
                }
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error
