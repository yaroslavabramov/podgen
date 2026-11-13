"""Bind run-local S labels and authorization dependencies to reusable document data."""

from podgen.domain.errors import Conflict
from podgen.persistence.database import one, query

from .source_search_index import WHERE


def register_dependencies(index, tx, hashes):
    missing = hashes - index.registered_dependencies
    if missing:
        query(
            tx,
            "INSERT INTO source_search_scope_dependencies SELECT :p,:r,sha FROM unnest(CAST(:hashes AS text[])) AS h(sha) ON CONFLICT DO NOTHING",
            p=index.params["p"],
            r=index.params["r"],
            hashes=sorted(missing),
        )


def bind(index, tx, source_id, key, hashes):
    register_dependencies(index, tx, hashes)
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
