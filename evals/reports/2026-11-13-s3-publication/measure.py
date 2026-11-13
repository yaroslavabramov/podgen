"""Reproduce complete save_blob before/after work without PostgreSQL or external S3.

PYTHONPATH=backend/src:backend backend/.venv/bin/python \
    evals/reports/2026-11-13-s3-publication/measure.py
"""

import json
import subprocess
import tempfile
import tracemalloc
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from podgen.persistence.objects import S3ObjectStore
from podgen.persistence.repository import Repository
from tests.blob_store_fakes import BlobDatabase, DiskS3
from tests.test_s3_publication_transport import GeneratedStream, transport_operation

BASELINE = "31b545cb"
ROOT = Path(__file__).resolve().parents[3]


def baseline(name):
    path = f"backend/src/podgen/persistence/{name}.py"
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:{path}"], cwd=ROOT, text=True
    )
    module = ModuleType(f"podgen.persistence.baseline_{name}")
    module.__package__ = "podgen.persistence"
    exec(compile(source, f"{BASELINE}/{path}", "exec"), module.__dict__)  # noqa: S102 - exact pinned baseline
    return module


def measure(repo_type, store_type, length):
    with tempfile.TemporaryDirectory() as directory:
        db = BlobDatabase()
        s3 = DiskS3(Path(directory) / "s3", db)
        s3.enforce_no_tx = False  # Record the historical violation rather than prevent its measurement.
        repo, project = repo_type(db, store_type(s3, "bucket"), uuid4()), uuid4()
        operations = []
        for _ in range(2):
            before, sql = dict(s3.counts), sum(db.queries.values())
            tracemalloc.start()
            source = GeneratedStream(length)
            repo.save_blob(project, source, "application/octet-stream")
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert source.remaining == 0
            operations.append(
                {
                    "s3": {
                        k: v - before.get(k, 0)
                        for k, v in s3.counts.items()
                        if v - before.get(k, 0)
                    },
                    "sql": sum(db.queries.values()) - sql,
                    "peak_traced_bytes": peak,
                    "input_bytes_read": length,
                }
            )
        return {
            "length": length,
            "operations": operations,
            "finalized_objects": len(s3.objects),
            "published_rows": len(db.rows),
        }


def main():
    old_repo, old_store = (
        baseline("repository").Repository,
        baseline("objects").S3ObjectStore,
    )
    report = {"baseline_commit": BASELINE, "sizes": {}}
    for length in (4 * 1024 * 1024, 64 * 1024 * 1024):
        report["sizes"][str(length)] = {
            "before": measure(old_repo, old_store, length),
            "after": measure(Repository, S3ObjectStore, length),
        }
    with pytest.MonkeyPatch.context() as patch:
        counts, sizes, peak = transport_operation(patch)
    report["native_boto3_local_http"] = {
        "counts": counts,
        "file_range_read_sizes": sizes,
        "peak_traced_bytes": peak,
    }
    destination = Path(__file__).with_name("measurements.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
