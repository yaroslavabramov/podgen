"""Bounded original-byte fingerprints for already computed, authorized passages."""

from hashlib import sha256

from podgen.persistence.database import query
from podgen.persistence.source_reading import read_sources
from podgen.retrieval import RetrievalUnavailable


def prepare(index, documents):
    reusable = index.passage_table == "source_search_document_chunks"
    if reusable:
        selection = """SELECT c.document_key AS owner,c.start_offset,c.end_offset,min(m.source_id) AS source_id
            FROM source_search_memberships m JOIN source_search_document_chunks c
              ON c.project_id=m.project_id AND c.document_key=m.document_key
            WHERE m.project_id=:p AND m.shared_input_record_id=:r AND m.profile_sha256=:h
              AND m.source_id=ANY(:scope) AND c.passage_sha256 IS NULL
            GROUP BY c.document_key,c.start_offset,c.end_offset
            ORDER BY c.document_key,c.start_offset LIMIT 64"""
    else:
        selection = """SELECT source_id AS owner,source_id,start_offset,end_offset
            FROM source_search_chunks WHERE project_id=:p AND shared_input_record_id=:r
              AND profile_sha256=:h AND source_id=ANY(:scope) AND passage_sha256 IS NULL
            ORDER BY source_id,start_offset LIMIT 64"""
    while True:
        with index.access.transaction() as (tx, _):
            rows = list(query(tx, selection, **index.params, scope=sorted(documents)).mappings())
        if not rows:
            return
        with read_sources([documents[row["source_id"]] for row in rows]):
            values = []
            for row in rows:
                text = documents[row["source_id"]].text[row["start_offset"] : row["end_offset"]]
                values.append(
                    (row["owner"], row["start_offset"], row["end_offset"], sha256(text.encode()).hexdigest())
                )
        parameters = dict(index.params)
        slots = []
        for number, value in enumerate(values):
            names = [f"v{number}_{column}" for column in range(4)]
            parameters.update(zip(names, value, strict=True))
            slots.append(
                "("
                + ",".join(
                    f"CAST(:{name} AS {kind})"
                    for name, kind in zip(names, ("text", "integer", "integer", "text"), strict=True)
                )
                + ")"
            )
        owner = "document_key" if reusable else "source_id"
        scope = "" if reusable else " AND c.shared_input_record_id=:r AND c.profile_sha256=:h"
        with index.access.transaction() as (tx, _):
            count = query(
                tx,
                f"UPDATE {index.passage_table} c SET passage_sha256=v.fingerprint FROM (VALUES "
                + ",".join(slots)
                + f") AS v(owner,start_offset,end_offset,fingerprint) WHERE c.project_id=:p AND c.{owner}=v.owner"
                + " AND c.start_offset=v.start_offset AND c.end_offset=v.end_offset AND (c.passage_sha256 IS NULL OR c.passage_sha256=v.fingerprint)"
                + scope,
                **parameters,
            ).rowcount
            if count != len(values):
                raise RetrievalUnavailable("source_search_passage_binding_changed")
