"""Compare actual grouped retrieval with saved original search diagnostics."""

import argparse
import hashlib
import json
import os
import resource
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import Database, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.persistence.source_reading import read_sources
from podgen.research.materials.loading import load_materials
from podgen.retrieval.assets import cache_root
from podgen.retrieval.config import PASSAGE_POLICY
from podgen.retrieval.onnx_models import models
from podgen.retrieval.streaming import StreamingSearch
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from podgen.writing.source_tools import SourceCorpus


def forbidden(*args, **kwargs):
    raise RuntimeError("External HTTP is disabled in saved-search qualification")


def selector(row):
    return tuple(row[k] for k in ("source_id", "start", "end"))


class LocalModels:
    def __init__(self):
        self.actual = models(str(cache_root()))
        self.counts = Counter()

    def offsets(self, text):
        return self.actual.offsets(text)

    def embed(self, texts):
        self.counts["passage_embeddings"] += len(texts)
        return self.actual.embed(texts)

    def query(self, text):
        self.counts["query_embeddings"] += 1
        return self.actual.query(text)

    def rerank(self, text, texts):
        self.counts["reranked_passages"] += len(texts)
        return self.actual.rerank(text, texts)


def main():
    parser = argparse.ArgumentParser()
    for name in ("state", "review", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    review = json.loads(args.review.read_text())
    output = args.out
    import urllib.request

    import httpx
    import httpx2

    httpx.Client.send = forbidden
    httpx2.Client.send = forbidden
    urllib.request.urlopen = forbidden
    database = Database(os.environ["PODGEN_DATABASE_URL"])
    repo = Repository(
        database, FileObjectStore(Path(state["objects"])), UUID(state["actor"]), purpose="evaluation"
    )
    journal = EvaluationJournal(repo, UUID(state["project"]))
    run_id = UUID(state["run_id"])
    manifest = journal.manifest(run_id)
    generation = Repository(database, repo.objects, repo.actor_id, purpose="generation")
    materials = load_materials(
        generation,
        journal.project,
        RuntimeRef.model_validate(manifest["author_ref"]),
        case_id=manifest["command"]["case_id"],
    )
    corpus = SourceCorpus(materials)
    documents = corpus.documents
    cases = [row for row in review["events"] if row["event_kind"] == "source_search_diagnostics"]
    assert len(cases) == 25
    legacy = cases[0]["data"]["retrieval_profile"]
    network = LocalModels()
    sql = Counter()

    def count(conn, cursor, statement, params, context, many):
        sql["calls"] += 1

    event.listen(database.engine, "before_cursor_execute", count)
    started = time.monotonic()
    old = StreamingSearch(
        documents,
        corpus.cards,
        deepcopy(legacy),
        index=StreamingSearchIndex(journal, materials.shared_ref, deepcopy(legacy), run_id=run_id),
        network=network,
    )
    from types import SimpleNamespace

    first = cases[0]["data"]
    request = SimpleNamespace(query=first["query"], within=first["within"], offset=0, result_version=None)
    pinned = old.search(request)
    assert [selector(h) for h in pinned["hits"]] == [
        selector(h) for h in first["ranked"][: len(pinned["hits"])]
    ]
    before_embeddings = network.counts["passage_embeddings"]
    policy = dict(legacy, retrieval_policy=PASSAGE_POLICY)
    new = StreamingSearch(
        documents,
        corpus.cards,
        policy,
        index=StreamingSearchIndex(journal, materials.shared_ref, policy, run_id=run_id),
        network=network,
    )
    assert network.counts["passage_embeddings"] == before_embeddings == 0
    preparation = dict(
        seconds=time.monotonic() - started,
        sql_calls=sql["calls"],
        passage_embeddings=before_embeddings,
        documents=len(documents),
    )
    results = []
    with database.read_transaction() as tx:
        chunks = {
            (r["source_id"], r["start_offset"], r["end_offset"])
            for r in query(
                tx,
                """SELECT m.source_id,c.start_offset,c.end_offset FROM source_search_memberships m JOIN source_search_document_chunks c USING(project_id,document_key) WHERE m.project_id=:p AND m.shared_input_record_id=:r AND m.profile_sha256=:h""",
                **new.index.params,
            ).mappings()
        }
    for number, case in enumerate(cases, 1):
        data = case["data"]
        arguments = SimpleNamespace(query=data["query"], within=data["within"], offset=0, result_version=None)
        before = sql["calls"]
        start = time.monotonic()
        answer = new.search(arguments)
        elapsed = time.monotonic() - start
        calls = sql["calls"] - before
        ranked = new.diagnostics(arguments)["ranked"]
        original = data["ranked"][:6]
        old_selectors = {selector(row) for row in original}
        alias_owner = {
            selector(alias): position for position, row in enumerate(ranked) for alias in row["aliases"]
        }
        merged = []
        newly = []
        with read_sources(documents.values()):
            for row in ranked:
                source = documents[row["source_id"]]
                text = source.text[row["start"] : row["end"]]
                for alias in row["aliases"]:
                    assert selector(alias) in chunks
                    raw = documents[alias["source_id"]].text[alias["start"] : alias["end"]]
                    rendered = text
                    edits = []
                    for variant in row.get("variants", []):
                        left, right = variant["versions"]
                        for version in (left, right):
                            assert (
                                documents[version["source_id"]].text[version["start"] : version["end"]]
                                == version["text"]
                            )
                        if left["source_id"] == row["source_id"] and right["source_id"] == alias["source_id"]:
                            edits.append(
                                (left["start"] - row["start"], left["end"] - row["start"], right["text"])
                            )
                    for lo, hi, replacement in sorted(edits, reverse=True):
                        rendered = rendered[:lo] + " " + replacement + " " + rendered[hi:]
                    assert " ".join(raw.split()) in " ".join(rendered.split()), (number, row, alias)
                if len(row["aliases"]) > 1 and row["rank"] <= 6:
                    merged.append(
                        dict(
                            source_id=row["source_id"],
                            start=row["start"],
                            end=row["end"],
                            aliases=row["aliases"],
                            text=text,
                        )
                    )
                if row["rank"] <= 6 and not any(selector(alias) in old_selectors for alias in row["aliases"]):
                    newly.append(
                        dict(source_id=row["source_id"], start=row["start"], end=row["end"], text=text)
                    )
            for hit in answer["hits"]:
                assert (
                    documents[hit["source_id"]].text[hit["preview_start"] : hit["preview_end"]]
                    == hit["preview"]
                )
            original_texts = [
                dict(
                    selector={k: r[k] for k in ("source_id", "start", "end")},
                    text=documents[r["source_id"]].text[r["start"] : r["end"]],
                )
                for r in original
            ]
        grouped_old = Counter(alias_owner.get(s, ("not_selected", s)) for s in old_selectors)
        freed = sum(max(0, n - 1) for n in grouped_old.values())
        result = dict(
            number=number,
            stage=case["stage"],
            query=data["query"],
            within=data["within"],
            before_top6=original_texts,
            after_page=answer,
            after_ranked_top6=ranked[:6],
            merged_groups=merged,
            newly_visible_passages=newly,
            redundant_old_top6_slots=freed,
            seconds=elapsed,
            sql_calls=calls,
        )
        results.append(result)
        (output / f"query-{number:02}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(
            json.dumps(
                dict(query=number, freed_slots=freed, new_passages=len(newly), seconds=round(elapsed, 3))
            ),
            flush=True,
        )
    assert (
        old.search(SimpleNamespace(**dict(vars(request), result_version=pinned["result_version"]))) == pinned
    )
    summary = dict(
        policy=PASSAGE_POLICY,
        queries=len(results),
        external_http_calls=0,
        preparation=preparation,
        inference=dict(network.counts),
        changed_top6_queries=sum(bool(r["redundant_old_top6_slots"]) for r in results),
        redundant_top6_slots=sum(r["redundant_old_top6_slots"] for r in results),
        newly_visible_passages=sum(len(r["newly_visible_passages"]) for r in results),
        all_original_alias_selectors_verified=True,
        all_group_content_verified=True,
        old_pinned_first_page_unchanged=True,
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        query_seconds=sum(r["seconds"] for r in results),
        query_sql_calls=sum(r["sql_calls"] for r in results),
        reference_sha256=hashlib.sha256(args.review.read_bytes()).hexdigest(),
    )
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    database.engine.dispose()


if __name__ == "__main__":
    main()
