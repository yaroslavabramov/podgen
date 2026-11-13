"""Bind run-local S labels and authorization dependencies to reusable document data."""

from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query

from .source_search_index import WHERE


def bind(index, tx, source_id, key, hashes):
    missing = hashes - index.registered_dependencies
    if missing:
        query(
            tx,
            "INSERT INTO source_search_scope_dependencies SELECT :p,:r,sha FROM unnest(CAST(:hashes AS text[])) AS h(sha) ON CONFLICT DO NOTHING",
            p=index.params["p"],
            r=index.params["r"],
            hashes=sorted(missing),
        )
    query(
        tx,
        "INSERT INTO source_search_memberships VALUES(:p,:r,:h,:s,:document) ON CONFLICT DO NOTHING",
        **index.params,
        s=source_id,
        document=key,
    )
    row = one(
        tx,
        "SELECT document_key FROM source_search_memberships WHERE " + WHERE + " AND source_id=:s",
        **index.params,
        s=source_id,
    )
    if row is None or row["document_key"] != key:
        raise Conflict("source_search_document_binding_changed")


def adopt_legacy(index, tx, source_id, key, doc, language):
    """Import an existing v2 build once, entirely in SQL, retaining its batch cursor."""
    row = one(
        tx,
        "SELECT i.source_sha256,i.source_length,i.profile,i.language,i.chunk_count,b.cursor_position,b.completed "
        "FROM source_search_indexes i JOIN source_search_builds b USING(project_id,shared_input_record_id,profile_sha256,source_id) WHERE "
        + " AND ".join("i." + part for part in WHERE.split(" AND "))
        + " AND i.source_id=:s",
        **index.params,
        s=source_id,
    )
    if row is None:
        return None
    if (row["source_sha256"], row["source_length"], row["profile"], row["language"]) != (
        doc.digest,
        len(doc.text),
        index.profile,
        language,
    ):
        raise Conflict("source_search_document_binding_changed")
    copied = query(
        tx,
        "INSERT INTO source_search_document_chunks SELECT project_id,:document,start_offset,end_offset,language,lexical,exact_terms,phrase_text,embedding "
        "FROM source_search_chunks WHERE " + WHERE + " AND source_id=:s",
        **index.params,
        s=source_id,
        document=key,
    ).rowcount
    if copied != row["chunk_count"]:
        raise Conflict("source_search_legacy_chunk_count_mismatch")
    query(
        tx,
        "UPDATE source_search_documents SET cursor_position=:cursor,completed=:done,chunk_count=:count WHERE project_id=:p AND document_key=:document",
        p=index.params["p"],
        document=key,
        cursor=row["cursor_position"],
        done=row["completed"],
        count=copied,
    )
    return dict(row)
