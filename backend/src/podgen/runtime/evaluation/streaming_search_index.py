"""Run-local search authority over reusable, atomically published document projections."""

import json
from contextlib import contextmanager

from sqlalchemy.exc import SQLAlchemyError

from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query
from podgen.retrieval import RetrievalUnavailable

from .document_projection import DocumentProjection, identity
from .legacy_source_index import adopt as adopt_legacy
from .source_search_index import WHERE, PostgresSearchIndex
from .source_search_membership import bind


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
        self.projections = DocumentProjection(self)
        self.registered_dependencies = set()
        self.document_keys = {}

    @contextmanager
    def transaction(self):
        try:
            with self.access.transaction() as current:
                yield current
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    @contextmanager
    def document(self, source_id, doc, language):
        key = identity(doc, self.profile, language)
        try:
            with self.projections.lock(key):
                yield self.begin_document(source_id, doc, language)
        except SQLAlchemyError as error:
            raise RetrievalUnavailable("source_search_index_unavailable") from error

    def begin_document(self, source_id, doc, language):
        self.access.source_hashes.add(doc.digest)
        key = identity(doc, self.profile, language)
        with self.transaction() as (tx, hashes):
            state = self._begin(tx, source_id, key, doc, language, hashes)
        # Cache registration only after commit; a failed transaction must retry it.
        self.registered_dependencies.update(hashes)
        self.document_keys[source_id] = key
        return state

    def _begin(self, tx, source_id, key, doc, language, hashes):
        state = self.projections.begin(tx, key, doc, language)
        bind(self, tx, source_id, key, hashes)
        if state["cursor_position"] == 0 and not state["completed"]:
            imported = adopt_legacy(self, tx, source_id, key, doc, language)
            if imported is not None:
                state = imported
        return state

    def prepare_documents(self, documents):
        """Bind one bounded metadata batch, reusing completed work before inference."""
        if len(documents) > 64:
            raise Conflict("source_search_prepare_batch_too_large")
        self.access.source_hashes.update(doc.digest for _, doc, _ in documents)
        ready, keys, locked = set(), {}, set()
        with self.transaction() as (tx, hashes):
            for source_id, doc, language in documents:
                key = identity(doc, self.profile, language)
                if key not in locked:
                    self.projections.lock_transaction(tx, key)
                    locked.add(key)
                state = self._begin(tx, source_id, key, doc, language, hashes if not keys else set())
                keys[source_id] = key
                if state["completed"]:
                    ready.add(source_id)
        self.registered_dependencies.update(hashes)
        self.document_keys.update(keys)
        return ready

    def append_batch(self, source_id, doc, ranges, language, vectors, *, previous, cursor, texts=None):
        key = identity(doc, self.profile, language)
        if self.document_keys.get(source_id) != key:
            raise Conflict("source_search_document_binding_changed")
        if texts is None:
            from podgen.persistence.source_reading import read_sources

            with read_sources([doc]):
                texts = [doc.text[start:end] for start, end in ranges]
        with self.transaction() as (tx, _):
            self.projections.append(
                tx, key, doc, ranges, language, vectors, previous=previous, cursor=cursor, texts=texts
            )

    def finish_empty(self, source_id):
        with self.transaction() as (tx, _):
            self.projections.finish_empty(tx, self.document_keys[source_id])

    def permitted(self):
        # Exhaustive branches operate over this run's labels and completed data.
        # Shared vectors never imply that another run may search a document.
        return (
            "WITH permitted AS (SELECT m.source_id,c.start_offset,c.end_offset,c.language,c.lexical,c.exact_terms,c.phrase_text,c.embedding "
            "FROM source_search_memberships m JOIN source_search_documents d ON d.project_id=m.project_id AND d.document_key=m.document_key "
            "JOIN source_search_document_chunks c ON c.project_id=d.project_id AND c.document_key=d.document_key WHERE "
            + " AND ".join("m." + part for part in WHERE.split(" AND "))
            + " AND m.source_id=ANY(:scope) AND d.completed) "
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
