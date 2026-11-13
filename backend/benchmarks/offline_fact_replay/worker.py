"""Replay all 43 saved turns, twice, through actual tools/persistence/export."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from export_oracle import ExportOracle
from metrics import Metrics
from traffic import Traffic

from podgen.domain.canonical import digest
from podgen.persistence.database import Database, query
from podgen.persistence.repository import Repository
from podgen.persistence.text_source import VerifiedText
from podgen.retrieval.onnx_models import Models
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects
from podgen.runtime.evaluation.fact_source_experiment.service import run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.model_turns import TurnJournal
from podgen.runtime.evaluation.operation_guard import OperationGuard
from podgen.runtime.models.research_context import ResearchContext
from podgen.runtime.models.research_tools import ResearchTools
from podgen.writing.source_tools import SourceCorpus

state = json.loads(Path(os.environ["PODGEN_REPLAY_STATE"]).read_text())
output = Path(os.environ["PODGEN_REPLAY_OUT"])
output.mkdir(parents=True, exist_ok=True)
database = Database(state["database_url"])
objects = MeasuredObjects(Path(state["objects"]))
repo = Repository(database, objects, UUID(state["actor"]), purpose="evaluation")
generation = Repository(database, objects, repo.actor_id, purpose="generation")
journal = EvaluationJournal(repo, UUID(state["project"]))
with database.read_transaction() as tx:
    legacy = dict(
        query(
            tx,
            """SELECT count(*) AS documents,coalesce(sum(i.chunk_count),0) AS chunks
        FROM source_search_indexes i JOIN source_search_builds b
        USING(project_id,shared_input_record_id,profile_sha256,source_id)
        WHERE i.project_id=:p AND i.shared_input_record_id=:r AND b.completed""",
            p=journal.project,
            r=uuid5(UUID(state["run_id"]), "source-search-scope/1"),
        )
        .mappings()
        .one()
    )
assert legacy == dict(documents=51, chunks=1227), legacy
metrics = Metrics(database, objects)
original_slice = VerifiedText.__getitem__
original_ensure = VerifiedText._ensure


def read_projection(self, key):
    result = original_slice(self, key)
    objects.counts.update(unicode_projection_read_calls=1, unicode_projection_returned_bytes=4 * len(result))
    return result


def ensure_projection(self):
    existing = self.path.exists()
    result = original_ensure(self)
    if not existing:
        objects.counts["unicode_projection_created_bytes"] += self.path.stat().st_size
    return result


VerifiedText.__getitem__ = read_projection
VerifiedText._ensure = ensure_projection

for cls, method in [
    (EvaluationJournal, "read"),
    (EvaluationJournal, "read_blob"),
    (EvaluationJournal, "prepare"),
    (EvaluationJournal, "transition"),
    (EvaluationJournal, "manifest"),
    (EvaluationJournal, "record"),
    (TurnJournal, "prepare"),
    (TurnJournal, "transition"),
    (Repository, "save_blob"),
    (OperationGuard, "__call__"),
    (ResearchContext, "measured"),
    (ResearchContext, "receipt"),
]:
    metrics.wrap(cls, method)
metrics.wrap(Models, "embed", lambda self, texts, **kw: "embedding:" + kw.get("prefix", "passage: ").strip())
metrics.wrap(EvaluationJournal, "event", lambda self, invocation, kind, **kw: "event:" + kind)
metrics.wrap(SourceCorpus, "execute", lambda self, name, *args, **kw: "tool:" + name)

original_request = ResearchContext.request


def context_request(self, body, index, segment_start):
    metrics.turn = index
    if index % 10 == 0:
        print("turn", index, flush=True)
    return original_request(self, body, index, segment_start)


ResearchContext.request = context_request
original_tool = ResearchTools.execute


def execute_tool(self, name, args, **options):
    result = original_tool(self, name, args, **options)
    expected = traffic.tool_results[traffic.seen_tools]
    assert digest(result) == digest(expected), ("tool_result_changed", name, traffic.seen_tools)
    traffic.seen_tools += 1
    return result


ResearchTools.execute = execute_tool
initial = UUID("a07cc6d1-c399-5979-a698-084ece565c47")
archive = os.environ["PODGEN_REPLAY_TRACE"]
export_oracle = ExportOracle(archive)
try:
    for label, run_id in (
        ("first", initial),
        ("repeat", uuid5(initial, "offline-repeat/1")),
        ("completed", initial),
    ):
        if label not in os.environ.get("PODGEN_REPLAY_PASSES", "first,repeat,completed").split(","):
            continue
        metrics.search_calls = 0
        metrics.search_seconds = 0
        metrics.phases = {}
        metrics.operations.clear()
        metrics.templates.clear()
        metrics.turns.clear()
        metrics.turn = None
        traffic = Traffic(archive, run_id)
        try:
            result = run(
                repo,
                generation,
                journal,
                UUID(state["run_id"]),
                run_id,
                traffic.provider,
                traffic.approved_hash,
                metrics,
                output / label,
            )
            expected = 0 if label == "completed" else 43
            assert len(traffic.seen_counts) == len(traffic.seen_submits) == expected
            assert traffic.seen_tools == (0 if label == "completed" else 42)
            (output / (label + ".json")).write_text(
                json.dumps(
                    dict(
                        metrics.snapshot(),
                        rss_scope="cumulative process high-water, not an independent per-pass peak",
                        input_legacy_corpus=legacy,
                        passage_embedding_calls=sum(
                            v.get("calls", 0)
                            for k, v in metrics.operations.items()
                            if k.endswith("embedding:passage:")
                        ),
                        export_verification=export_oracle.verify(output / label / "export"),
                        count_calls=expected,
                        submit_calls=expected,
                        tool_results_exact=traffic.seen_tools,
                        all_native_requests_exact=True,
                    ),
                    indent=2,
                )
            )
            print(label, json.dumps(metrics.phases), flush=True)
        finally:
            traffic.client.close()
finally:
    database.engine.dispose()
