"""Adopt content-identical legacy computation inside an authorized destination transaction."""

import json

from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query

LEGACY = "project_id=:p AND shared_input_record_id=:legacy_scope AND profile_sha256=:h AND source_id=:legacy_source"


def adopt(index, tx, source_id, key, doc, language):
    # Completed computation belongs to content/configuration, not a prior run's
    # label. A partial legacy build is resumed only in its original scope.
    selector = (
        "SELECT i.shared_input_record_id,i.source_id,i.chunk_count,b.cursor_position,b.completed "
        "FROM source_search_indexes i JOIN source_search_builds b "
        "USING(project_id,shared_input_record_id,profile_sha256,source_id) "
        "WHERE i.project_id=:p AND i.source_sha256=:sha AND i.source_length=:length "
        "AND i.profile_sha256=:h AND i.profile=CAST(:profile AS jsonb) AND i.language=:language "
    )
    parameters = dict(
        **index.params,
        s=source_id,
        sha=doc.digest,
        length=len(doc.text),
        profile=json.dumps(index.profile),
        language=language,
    )
    # Any completed exact-content candidate is equivalent. Avoid sorting every
    # historical occurrence of the same document before taking one match.
    row = one(
        tx,
        selector + "AND b.completed AND b.cursor_position=i.source_length LIMIT 1 FOR SHARE OF i,b",
        **parameters,
    )
    if row is None:
        row = one(
            tx,
            selector + "AND NOT b.completed AND b.cursor_position<i.source_length "
            "AND i.shared_input_record_id=:r AND i.source_id=:s FOR SHARE OF i,b",
            **parameters,
        )
    if row is None:
        return None
    params = dict(
        p=index.params["p"],
        h=index.params["h"],
        legacy_scope=row["shared_input_record_id"],
        legacy_source=row["source_id"],
    )
    # Validate actual chunk membership, coordinates and language before copying;
    # descriptor equality alone does not establish a complete legacy projection.
    chunks = one(
        tx,
        "SELECT count(*) AS actual_count,coalesce(bool_and(start_offset>=0 AND "
        "end_offset>start_offset AND end_offset<=:cursor AND language=:language),true) AS valid "
        "FROM source_search_chunks WHERE " + LEGACY,
        **params,
        cursor=row["cursor_position"],
        language=language,
    )
    if chunks["actual_count"] != row["chunk_count"] or not chunks["valid"]:
        raise Conflict("source_search_legacy_chunk_count_mismatch")
    copied = query(
        tx,
        "INSERT INTO source_search_document_chunks "
        "SELECT project_id,:document,start_offset,end_offset,language,lexical,exact_terms,phrase_text,embedding "
        "FROM source_search_chunks WHERE " + LEGACY,
        **params,
        document=key,
    ).rowcount
    if copied != row["chunk_count"]:
        raise Conflict("source_search_legacy_chunk_count_mismatch")
    query(
        tx,
        "UPDATE source_search_documents SET cursor_position=:cursor,completed=:done,chunk_count=:count "
        "WHERE project_id=:p AND document_key=:document",
        p=index.params["p"],
        document=key,
        cursor=row["cursor_position"],
        done=row["completed"],
        count=copied,
    )
    return dict(row)
