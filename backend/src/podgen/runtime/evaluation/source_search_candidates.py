"""Shared exhaustive branches; a versioned policy groups exact text before caps."""

import json

from sqlalchemy.exc import SQLAlchemyError

from podgen.persistence.database import query
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.chunks import Chunk
from podgen.retrieval.lexical import TOKEN, words

CONFIG = "CASE language WHEN 'en' THEN 'english'::regconfig WHEN 'ru' THEN 'russian'::regconfig ELSE 'simple'::regconfig END"


def candidates(index, text, vector, allowed, limit, *, group_exact):
    params = dict(
        index.params,
        scope=sorted(allowed),
        q=text,
        vector=json.dumps(vector.tolist()),
        limit=limit,
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
    selector = "source_id,start_offset,end_offset," + ("passage_sha256," if group_exact else "")
    branches = dict(
        dense=f"SELECT {selector}1-(embedding <=> CAST(:vector AS vector)) AS score FROM permitted",
        lexical=f"SELECT {selector}ts_rank_cd(lexical,websearch_to_tsquery({CONFIG},:lexical_query)) AS score FROM permitted WHERE lexical @@ websearch_to_tsquery({CONFIG},:lexical_query)",
        exact=f"SELECT {selector}((strpos(' '||phrase_text||' ',:phrase)>0)::int + (SELECT count(*)::float FROM unnest(CAST(:entities AS text[])) AS e(term) WHERE e.term=ANY(exact_terms))/GREATEST(cardinality(CAST(:entities AS text[])),1)) AS score FROM permitted WHERE strpos(' '||phrase_text||' ',:phrase)>0 OR exact_terms && CAST(:entities AS text[])",
    )
    result = {}
    try:
        with index.access.transaction() as (tx, _):
            for name, base in branches.items():
                if group_exact:
                    statement = index.permitted() + ", scored AS (" + base + "), selected AS MATERIALIZED ("
                    statement += "SELECT passage_sha256,max(score) AS score,min(source_id) AS first_source,min(start_offset) AS first_start FROM scored GROUP BY passage_sha256 ORDER BY score DESC,first_source,first_start,passage_sha256 LIMIT :limit) "
                    statement += "SELECT s.passage_sha256,s.score,jsonb_agg(jsonb_build_object('source_id',p.source_id,'start',p.start_offset,'end',p.end_offset) ORDER BY p.source_id,p.start_offset,p.end_offset) AS aliases FROM selected s JOIN permitted p ON p.passage_sha256 IS NOT DISTINCT FROM s.passage_sha256 GROUP BY s.passage_sha256,s.score,s.first_source,s.first_start ORDER BY s.score DESC,s.first_source,s.first_start,s.passage_sha256"
                    rows = list(query(tx, statement, **params).mappings())
                    if any(row["passage_sha256"] is None for row in rows):
                        raise RetrievalUnavailable("source_search_passage_fingerprint_missing")
                    result[name] = [
                        (
                            Chunk(**row["aliases"][0]),
                            float(row["score"]),
                            tuple(Chunk(**alias) for alias in row["aliases"]),
                        )
                        for row in rows
                    ]
                else:
                    order = "embedding <=> CAST(:vector AS vector)" if name == "dense" else "score DESC"
                    rows = query(
                        tx,
                        index.permitted() + base + f" ORDER BY {order},source_id,start_offset LIMIT :limit",
                        **params,
                    ).mappings()
                    result[name] = [
                        (Chunk(row["source_id"], row["start_offset"], row["end_offset"]), float(row["score"]))
                        for row in rows
                    ]
        return result
    except SQLAlchemyError as error:
        raise RetrievalUnavailable("source_search_index_unavailable") from error
