"""Publish bounded chunk batches; incomplete documents are never searchable."""

import json
from contextlib import contextmanager

from sqlalchemy.exc import SQLAlchemyError

from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.lexical import words

from .source_search_index import WHERE, PostgresSearchIndex


class StreamingSearchIndex(PostgresSearchIndex):
    def __init__(self, journal, shared_input_ref, profile, *, run_id):
        from uuid import uuid5

        scope = journal.record(
            dict(
                contract="run-source-search-scope/1",
                run_id=str(run_id),
                shared_input_ref=shared_input_ref.model_dump(mode="json"),
            ),
            record_id=uuid5(run_id, "source-search-scope/1"),
        )
        super().__init__(journal, scope, profile)

    @contextmanager
    def transaction(self, source_id=None):
        try:
            with self.access.transaction() as (tx, hashes):
                if source_id is not None:
                    query(
                        tx,
                        "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                        key="streaming-search:" + ":".join(map(str, self.params.values())) + source_id,
                    )
                yield tx, hashes
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    def begin_document(self, source_id, doc, language):
        self.access.source_hashes.add(doc.digest)
        with self.transaction(source_id) as (tx, hashes):
            row = one(
                tx,
                "SELECT * FROM source_search_indexes WHERE " + WHERE + " AND source_id=:s",
                **self.params,
                s=source_id,
            )
            if row is None:
                query(
                    tx,
                    "INSERT INTO source_search_indexes VALUES(:p,:r,:h,:s,:sha,:length,CAST(:profile AS jsonb),'[]'::jsonb,:language,0)",
                    **self.params,
                    s=source_id,
                    sha=doc.digest,
                    length=len(doc.text),
                    profile=json.dumps(self.profile),
                    language=language,
                )
                query(
                    tx,
                    "INSERT INTO source_search_builds(project_id,shared_input_record_id,profile_sha256,source_id) VALUES(:p,:r,:h,:s)",
                    **self.params,
                    s=source_id,
                )
                for fingerprint in hashes:
                    query(
                        tx,
                        "INSERT INTO source_search_dependencies VALUES(:p,:r,:h,:s,:sha)",
                        **self.params,
                        s=source_id,
                        sha=fingerprint,
                    )
            elif (row["source_sha256"], row["source_length"], row["profile"], row["language"]) != (
                doc.digest,
                len(doc.text),
                self.profile,
                language,
            ):
                raise Conflict("source_search_document_binding_changed")
            state = one(
                tx,
                "SELECT * FROM source_search_builds WHERE " + WHERE + " AND source_id=:s",
                **self.params,
                s=source_id,
            )
            if state is None:
                raise Conflict("source_search_build_missing")
            return dict(state)

    def append_batch(self, source_id, doc, ranges, language, vectors, *, previous, cursor):
        with self.transaction(source_id) as (tx, hashes):
            row = one(
                tx,
                "SELECT * FROM source_search_builds WHERE " + WHERE + " AND source_id=:s",
                **self.params,
                s=source_id,
            )
            if row is None or row["completed"] or row["cursor_position"] != previous:
                raise RetrievalUnavailable("source_search_build_cursor_changed")
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
            query(
                tx,
                "UPDATE source_search_indexes SET chunk_count=chunk_count+:count WHERE "
                + WHERE
                + " AND source_id=:s",
                **self.params,
                s=source_id,
                count=len(ranges),
            )
            query(
                tx,
                "UPDATE source_search_builds SET cursor_position=:cursor,completed=:done WHERE "
                + WHERE
                + " AND source_id=:s",
                **self.params,
                s=source_id,
                cursor=cursor,
                done=cursor == len(doc.text),
            )

    def finish_empty(self, source_id):
        with self.transaction(source_id) as (tx, _):
            query(
                tx,
                "UPDATE source_search_builds SET completed=true WHERE "
                + WHERE
                + " AND source_id=:s AND cursor_position=0",
                **self.params,
                s=source_id,
            )

    def permitted(self):
        # No corpus-sized Python collection or materialized vector CTE. PostgreSQL
        # evaluates the exhaustive scoped branches and returns only bounded candidates.
        return (
            "WITH permitted AS (SELECT c.* FROM source_search_chunks c WHERE "
            + " AND ".join("c." + part for part in WHERE.split(" AND "))
            + " AND source_id=ANY(:scope) AND EXISTS(SELECT 1 FROM source_search_builds b "
            "WHERE b.project_id=c.project_id AND b.shared_input_record_id=c.shared_input_record_id "
            "AND b.profile_sha256=c.profile_sha256 AND b.source_id=c.source_id AND b.completed)) "
        )

    def result(self, key, request, rows=None):
        with self.transaction() as (tx, _):
            if rows is not None:
                query(
                    tx,
                    "INSERT INTO source_search_results VALUES(:p,:r,:h,:key,CAST(:request AS jsonb),CAST(:rows AS jsonb)) ON CONFLICT DO NOTHING",
                    **self.params,
                    key=key,
                    request=json.dumps(request),
                    rows=json.dumps(rows),
                )
            saved = one(
                tx,
                "SELECT request,results FROM source_search_results WHERE " + WHERE + " AND query_sha256=:key",
                **self.params,
                key=key,
            )
            if saved is not None and saved["request"] != request:
                raise Conflict("source_search_result_binding_changed")
            return saved["results"] if saved is not None else None

    def result_version(self, key, text, within):
        with self.transaction() as (tx, _):
            row = one(
                tx,
                "SELECT request,results FROM source_search_results WHERE " + WHERE + " AND query_sha256=:key",
                **self.params,
                key=key,
            )
            if row is None or row["request"]["query"] != text or row["request"]["within"] != within:
                raise Conflict("source_search_result_version_mismatch")
            return row["results"]
