"""Measure two complete indexing/search operations before/after document reuse.

Requires an isolated, migrated PostgreSQL and the shared Podgen heavy-test slot.
Default inference is a counted deterministic double; --real-models uses installed
local ONNX weights only. No provider APIs or generated episodes are invoked.
"""

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import tracemalloc
import types
from collections import Counter
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import numpy as np
from sqlalchemy import event

from podgen.domain.contracts.common import BlobRef
from podgen.persistence.database import Database, one, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.retrieval.assets import cache_root
from podgen.retrieval.config import streaming_profile
from podgen.retrieval.lexical import language
from podgen.retrieval.onnx_models import models
from podgen.retrieval.streaming import StreamingSearch
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex

BASELINE = "b3d49764a712555a7c41944fd52bb17b76e5007c"


class CountedModels:
    def __init__(self, real=False):
        self.real = models(str(cache_root())) if real else None
        self.document_chunks = self.embedding_batches = self.query_calls = 0
        self.tokenized_characters = self.largest_window = 0

    def offsets(self, text):
        self.tokenized_characters += len(text)
        self.largest_window = max(self.largest_window, len(text))
        if self.real:
            return self.real.offsets(text)
        return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]

    def embed(self, texts):
        self.document_chunks += len(texts)
        self.embedding_batches += 1
        if self.real:
            return self.real.embed(texts)
        vectors = np.zeros((len(texts), 384), dtype=np.float32)
        vectors[:, 0] = 1
        return vectors

    def query(self, text):
        self.query_calls += 1
        if self.real:
            return self.real.query(text)
        return np.eye(1, 384, dtype=np.float32)[0]

    def rerank(self, text, candidates):
        return self.real.rerank(text, candidates) if self.real else [0.5] * len(candidates)


def historical_class(repository, package, relative, name):
    source = subprocess.run(
        ["git", "show", f"{BASELINE}:{relative}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    module = types.ModuleType(package + "._reusable_index_baseline")
    module.__package__ = package
    exec(compile(source, f"{BASELINE}:{relative}", "exec"), module.__dict__)
    return getattr(module, name)


def storage(database, project):
    result = {}
    with database.transaction() as tx:
        for table in ("source_search_chunks", "source_search_document_chunks"):
            result[table] = dict(
                one(
                    tx,
                    f"SELECT count(*) AS rows,COALESCE(sum(pg_column_size(embedding)),0) AS vector_bytes FROM {table} WHERE project_id=:p",
                    p=project,
                )
            )
    return result


def measure(database, journal, reference, documents, network, search_type, index_type):
    counts = Counter()

    def statement(_conn, _cursor, sql, parameters, _context, _many):
        counts["sql_calls"] += 1
        counts["sql_" + sql.split()[0].lower()] += 1
        # Diagnostic serialization size, not measured PostgreSQL wire traffic.
        counts["bound_parameter_repr_bytes"] += len(repr(parameters).encode())

    before = (
        network.document_chunks,
        network.embedding_batches,
        network.query_calls,
        network.tokenized_characters,
    )
    event.listen(database.engine, "before_cursor_execute", statement)
    tracemalloc.start()
    started = time.monotonic()
    try:
        index = index_type(journal, reference, streaming_profile(), run_id=uuid4())
        search = search_type(documents, {}, streaming_profile(), index=index, network=network)
        page = search.search(
            SimpleNamespace(query="bacteria source", within=None, offset=0, result_version=None)
        )
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        event.remove(database.engine, "before_cursor_execute", statement)
    return dict(
        counts,
        elapsed_seconds=round(elapsed, 3),
        python_tracemalloc_peak_bytes=peak,
        document_chunks_embedded=network.document_chunks - before[0],
        document_embedding_batches=network.embedding_batches - before[1],
        query_embedding_calls=network.query_calls - before[2],
        tokenized_characters=network.tokenized_characters - before[3],
        largest_tokenization_window=network.largest_window,
        returned_hits=len(page["hits"]),
        stored=storage(database, journal.project),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("PODGEN_TEST_DATABASE_URL"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--documents", type=int, default=62)
    parser.add_argument("--characters", type=int, default=881277)
    parser.add_argument("--real-models", action="store_true")
    parser.add_argument("--saved-inputs", type=Path)
    parser.add_argument("--object-root", type=Path)
    args = parser.parse_args()
    if not args.database_url:
        parser.error("Supply an isolated migrated test database")
    if args.documents < 1 or args.characters < args.documents * 64:
        parser.error("Supply positive document count and at least 64 characters per document")
    root = Path(__file__).resolve().parents[2]
    old_index = historical_class(
        root,
        "podgen.runtime.evaluation",
        "backend/src/podgen/runtime/evaluation/streaming_search_index.py",
        "StreamingSearchIndex",
    )
    old_search = historical_class(
        root, "podgen.retrieval", "backend/src/podgen/retrieval/streaming.py", "StreamingSearch"
    )
    database = Database(args.database_url)
    project, actor = uuid4(), uuid4()
    network = CountedModels(args.real_models)
    if args.saved_inputs:
        if args.object_root is None:
            parser.error("Saved inputs require their local object root")
        exported = json.loads(args.saved_inputs.read_text())
        by_path = {row["path"]: row for row in exported["documents"]}
        specs = {row["key"]: row for row in exported["editorial_input"]["documents"]}
        store = FileObjectStore(args.object_root)
        documents = {}
        for source in exported["editorial_input"]["sources"]:
            if source["document"] is None:
                continue
            spec = specs[source["document"]]
            saved = by_path[spec["source_path"]]
            blob = BlobRef.model_validate(saved["blob"])
            text = store.read(UUID(blob.storage_key.split("/")[0]), blob).decode("utf-8")
            documents[source["source_id"]] = SimpleNamespace(
                title=source["title"], text=text, digest=blob.sha256
            )
            if len(documents) == args.documents:
                break
        if not documents:
            parser.error("No retained documents found")
    else:
        paragraph = "The retained bacteria source explains an experiment, its observations and limitations. Моряк сравнивает время и проверяет точность.\n"
        documents = {}
        for number in range(args.documents):
            length = args.characters // args.documents + (number < args.characters % args.documents)
            text = (f"Source {number}: " + paragraph * (length // len(paragraph) + 1))[:length]
            documents[f"S{number + 1}"] = SimpleNamespace(
                title=f"Record {number}", text=text, digest=sha256(text.encode()).hexdigest()
            )
    # Keep one-time language/model initialization outside all compared operations.
    for doc in documents.values():
        language(doc.text[:16000])
    with tempfile.TemporaryDirectory(prefix="podgen-index-measurement-") as directory:
        repo = Repository(database, FileObjectStore(Path(directory)), actor, purpose="evaluation")
        with database.transaction() as tx:
            query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=project)
            query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=project, a=actor)
        try:
            journal = EvaluationJournal(repo, project)
            reference = journal.record(dict(contract="reusable-index-measurement/1"))
            rows = {}
            for label, search_type, index_type in (
                ("before", old_search, old_index),
                ("after", StreamingSearch, StreamingSearchIndex),
            ):
                for run in (1, 2):
                    rows[f"{label}_run_{run}"] = measure(
                        database, journal, reference, documents, network, search_type, index_type
                    )
            report = dict(
                baseline_commit=BASELINE,
                documents=len(documents),
                source_corpus="saved_full_documents" if args.saved_inputs else "synthetic",
                characters=sum(len(d.text) for d in documents.values()),
                inference="local_onnx" if args.real_models else "counted_double",
                scope="complete index construction plus first real SQL search; preloaded source texts, not source-loading/agent guard",
                limitations=[
                    "Python allocation peak excludes native/DB memory",
                    "Parameter bytes are repr size, not network measurements",
                    "No external provider calls; authorization graph here is a minimal admitted input",
                ],
                operations=rows,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            # Immutable benchmark records remain until the isolated database is discarded.
            database.engine.dispose()


if __name__ == "__main__":
    main()
