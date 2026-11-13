"""Compare the complete evaluator append boundary, against the pinned former classifier.

Run only in an isolated migrated test database. Baseline disables only the two new
publication triggers inside a rolled-back transaction; it does not modify durable
history. No model or object-store request is made.
"""

import argparse
import json
import subprocess
import time
import tracemalloc
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence import classification
from podgen.persistence.database import Database, json_text, one, query
from podgen.persistence.repository import Repository
from podgen.persistence.runtime_records import append_runtime
from sqlalchemy import text
from tests.privacy_graph_cases import Graph, checksum
from tests.privacy_projection_support import seed_graph

BASE = "a5e8d314"


def baseline():
    modules = []
    for name in ("privacy", "classification"):
        source = subprocess.check_output(
            ["git", "show", f"{BASE}:backend/src/podgen/persistence/{name}.py"],
            text=True,
        )
        module = ModuleType(f"podgen.persistence._baseline_{name}")
        exec(compile(source, f"{BASE}/{name}.py", "exec"), module.__dict__)  # noqa: S102 -- pinned repository source
        modules.append(module)
    modules[1].protected_hashes = modules[0].protected_hashes
    return modules[1].classify_evaluator


class Rows:
    def __init__(self, rows, result):
        self.rows, self.result = rows, result

    def record(self, row):
        if row is not None:
            metric = self.result.metric
            metric["selected_rows"] += 1
            metric["selected_value_json_bytes"] += len(
                json.dumps(dict(row), default=str, ensure_ascii=False).encode()
            )
            if "payload" in row:
                metric["full_runtime_payload_rows"] += 1
                metric["full_runtime_payload_bytes"] += len(
                    json_text(row["payload"]).encode()
                )
        return row

    def first(self):
        return self.record(self.rows.first())

    def __iter__(self):
        for row in self.rows:
            yield self.record(row)


class Result:
    def __init__(self, result, metric):
        self.result, self.metric = result, metric

    def mappings(self):
        return Rows(self.result.mappings(), self)

    def __enter__(self):
        self.result.__enter__()
        return self

    def __exit__(self, *args):
        return self.result.__exit__(*args)


class Connection:
    def __init__(self, connection, metric):
        self.connection, self.metric = connection, metric

    def execute(self, statement, parameters, **kwargs):
        self.metric["sql_statements"] += 1
        if "INSERT INTO evaluator_blob_classifications" in str(statement):
            self.metric["classification_inserts"] += 1
        return Result(
            self.connection.execute(statement, parameters, **kwargs), self.metric
        )


def measure(db, legacy, unrelated, reachable, reference_shape="one_blob"):
    outcomes = []
    for label, classifier in (
        ("before", legacy),
        ("after", classification.classify_evaluator),
    ):
        with db.engine.connect() as tx:
            transaction = tx.begin()
            try:
                if label == "before":
                    query(
                        tx,
                        "ALTER TABLE runtime_records DISABLE TRIGGER runtime_reference_projection",
                    )
                    query(
                        tx,
                        "ALTER TABLE evaluator_blob_classifications DISABLE TRIGGER evaluator_hash_membership",
                    )
                project, actor = uuid4(), uuid4()
                query(
                    tx,
                    "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')",
                    p=project,
                )
                query(
                    tx,
                    "INSERT INTO project_members VALUES(:p,:a,'owner')",
                    p=project,
                    a=actor,
                )
                roots = [uuid4() for _ in range(reachable)]
                graph = Graph(runtime_blobs={(r, checksum(1)) for r in roots})
                graph.runtime_blobs.update(
                    (r, checksum(n + 2)) for n, r in enumerate(roots)
                )
                seed_graph(tx, project, graph)
                unrelated_blob = {
                    "storage_key": f"{project}/{checksum(99999)}",
                    "sha256": checksum(99999),
                    "byte_length": 1,
                    "media_type": "test/synthetic",
                }
                query(
                    tx,
                    "INSERT INTO blobs VALUES(:p,:key,:sha,1,'test/synthetic')",
                    p=project,
                    key=unrelated_blob["storage_key"],
                    sha=unrelated_blob["sha256"],
                )
                # Unrelated text has no path to the measured evaluator event.
                tx.execute(
                    text(
                        "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')"
                    ),
                    [
                        {
                            "p": project,
                            "r": uuid4(),
                            "body": json_text(
                                {"text": "x" * 4096, "blob": unrelated_blob}
                            ),
                        }
                        for _ in range(unrelated)
                    ],
                )
                repo = Repository(db, None, actor, purpose="evaluation")
                blob = {
                    "storage_key": f"{project}/{checksum(1)}",
                    "sha256": checksum(1),
                    "byte_length": 1,
                    "media_type": "test/synthetic",
                }
                for iteration in range(2):
                    before = one(
                        tx,
                        "SELECT count(*) AS n FROM evaluator_blob_classifications WHERE project_id=:p",
                        p=project,
                    )["n"]
                    metrics = {
                        "sql_statements": 0,
                        "classification_inserts": 0,
                        "selected_rows": 0,
                        "selected_value_json_bytes": 0,
                        "full_runtime_payload_rows": 0,
                        "full_runtime_payload_bytes": 0,
                    }
                    ref = RuntimeRef(record_id=uuid4(), record_kind="tool_execution")
                    tracemalloc.start()
                    started = time.perf_counter()
                    with patch.object(classification, "classify_evaluator", classifier):
                        append_runtime(
                            repo,
                            Connection(tx, metrics),
                            project,
                            ref,
                            {"count_response": blob}
                            if reference_shape == "one_blob"
                            else {"status": "synthetic-empty"},
                            audience="evaluator",
                        )
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    after = one(
                        tx,
                        "SELECT count(*) AS n FROM evaluator_blob_classifications WHERE project_id=:p",
                        p=project,
                    )["n"]
                    actual = {
                        row["sha256"]
                        for row in query(
                            tx,
                            "SELECT sha256 FROM evaluator_blob_classifications WHERE project_id=:p",
                            p=project,
                        ).mappings()
                    }
                    assert actual == (
                        {checksum(n + 1) for n in range(reachable + 1)}
                        if reference_shape == "one_blob"
                        else set()
                    )
                    outcomes.append(
                        dict(
                            version=label,
                            reference_shape=reference_shape,
                            unrelated_records=unrelated,
                            reachable_preexisting_records=reachable,
                            protected_hashes=len(actual),
                            iteration=iteration + 1,
                            new_classification_rows=after - before,
                            elapsed_seconds=elapsed,
                            peak_python_bytes=peak,
                            **metrics,
                        )
                    )
            finally:
                transaction.rollback()
    return outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    db = Database(json.loads(args.state.read_text())["database_url"])
    legacy = baseline()
    rows = []
    try:
        for unrelated, reachable in ((64, 32), (1024, 32), (1024, 320)):
            rows.extend(measure(db, legacy, unrelated, reachable))
        rows.extend(measure(db, legacy, 1024, 32, "empty"))
    finally:
        db.engine.dispose()
    args.output.write_text(
        json.dumps({"baseline_commit": BASE, "measurements": rows}, indent=2) + "\n"
    )
    print(args.output)


if __name__ == "__main__":
    main()
