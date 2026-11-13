"""Internal isolated measurement worker; invoked by measure_operation.py only."""

import json
import os
import platform
import re
import resource
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository, timestamp
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import WritingRunCommand, command_payload
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import load_profile
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.evaluation.source_tool_recovery import TurnEvidence
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.source_tools import SourceCorpus
from sqlalchemy import event

state = json.loads(Path(os.environ["PODGEN_PROBE_STATE"]).read_text())


class Store(FileObjectStore):
    def __init__(self, root):
        super().__init__(root)
        self.metrics = Counter()

    def read(self, project, ref):
        value = super().read(project, ref)
        self.metrics["read_calls"] += 1
        self.metrics["read_bytes"] += len(value)
        return value

    def iter_read(self, project, ref, **kw):
        self.metrics["stream_reads"] += 1
        for chunk in super().iter_read(project, ref, **kw):
            self.metrics["read_bytes"] += len(chunk)
            yield chunk

    def put(self, project, stream, media_type, **kw):
        value = super().put(project, stream, media_type, **kw)
        self.metrics["put_calls"] += 1
        self.metrics["put_bytes"] += value.byte_length
        return value

    @contextmanager
    def stage(self, project, stream, media_type, **kw):
        with super().stage(project, stream, media_type, **kw) as staged:
            self.metrics["stage_calls"] += 1
            self.metrics["staged_input_bytes"] += staged.byte_length
            yield staged

    def publish(self, project, staged):
        self.metrics["publish_calls"] += 1
        self.metrics["publish_input_bytes"] += staged.byte_length
        return super().publish(project, staged)

    def verify_existing(self, project, ref, staged):
        self.metrics["verify_existing_calls"] += 1
        return super().verify_existing(project, ref, staged)


store = Store(Path(os.environ["PODGEN_PROBE_OBJECTS"]))
repo = Repository(
    Database(os.environ["PODGEN_TEST_DATABASE_URL"]),
    store,
    UUID(state["actor"]),
    purpose="evaluation",
)
j = EvaluationJournal(repo, UUID(state["project"]))
original = j.invocation(UUID(state["run_id"]), "plan")
manifest = j.manifest(UUID(state["run_id"]))
command = WritingRunCommand.model_validate(manifest["command"]).model_copy(
    update={"run_id": uuid4()}
)
newmanifest = dict(
    manifest, command=command_payload(command), created_at=timestamp(), stages=["plan"]
)
j.create_run(command, newmanifest)
body = j.request(original)
body["metadata"].update(podgen_run_id=str(command.run_id))
original_record = j.read(
    __import__("podgen.domain.contracts.common", fromlist=["RuntimeRef"]).RuntimeRef(
        record_kind="tool_execution", record_id=original["request_record_id"]
    )
)
root = j.prepare(command.run_id, "plan", body, original_record["context"])
profile = load_profile(repo, command.project_id, command.planner_profile_ref, "plan")
assert profile.provider == "openai", (
    "This probe requires a saved OpenAI planning invocation."
)
evaluation = WritingEvaluation(repo, lambda _: None)
materials = evaluation._materials(command)
corpus = SourceCorpus(materials)
source_id = next(iter(corpus.documents))
calls = Counter()
sql = Counter()
phases = Counter()
statements = Counter()
phase_sql = Counter()


def capture(conn, cursor, statement, parameters, context, many):
    sql["statements"] += 1
    if (
        "SELECT * FROM runtime_records WHERE" in statement
        or "SELECT payload FROM runtime_records WHERE" in statement
    ):
        sql["full_runtime_fetches"] += 1
    if os.environ.get("PODGEN_PROBE_BREAKDOWN"):
        frame = sys._getframe()
        labels = []
        while frame:
            filename = frame.f_code.co_filename
            function = frame.f_code.co_name
            if "/podgen/" in filename:
                module = filename.split("/podgen/")[-1]
                if module == "runtime/evaluation/journal.py" and function == "event":
                    labels.append("event:" + str(frame.f_locals.get("kind")))
                elif module == "runtime/evaluation/journal.py" and function == "record":
                    labels.append("journal.record")
                elif module == "persistence/repository.py" and function == "save_blob":
                    labels.append("save_blob")
                elif (
                    module == "runtime/evaluation/operation_guard.py"
                    and function == "__call__"
                ):
                    labels.append("guard")
                elif (
                    module == "runtime/evaluation/model_turns.py"
                    and function == "prepare"
                ):
                    labels.append("turn.prepare")
                elif (
                    module == "runtime/evaluation/journal.py" and function == "request"
                ):
                    labels.append("journal.request")
                elif (
                    module == "runtime/models/count_receipts.py"
                    and function == "validate"
                ):
                    labels.append("receipt.validate")
                elif (
                    module == "runtime/models/gateway.py" and function == "invoke_once"
                ):
                    labels.append("physical.preflight")
                elif (
                    module == "writing/source_tools/corpus.py"
                    and function == "_execute"
                ):
                    labels.append("tool.read")
                elif (
                    module == "runtime/evaluation/history_metadata.py"
                    and function == "stream"
                ):
                    labels.append("history.restore")
            frame = frame.f_back
        # Most specific caller, plus publication owner when blob upload is nested.
        label = " > ".join(dict.fromkeys(labels)) or "other"
        phases[label] += 1
        normalized = re.sub(r"\s+", " ", statement).strip()
        statements[normalized] += 1
        phase_sql[label, normalized] += 1


class ProbeStop(Exception):
    pass


def checkpoint():
    if calls["submit"] and any(
        row["event_kind"] == "source_tool_result" for row in j.events(root)
    ):
        raise ProbeStop()


try:
    from podgen.runtime.evaluation.operation_guard import OperationGuard
except ImportError:
    OperationGuard = None
boundary = (
    OperationGuard(
        generation=evaluation.generation,
        journal=j,
        command=command,
        manifest=newmanifest,
        materials=materials,
        loader=evaluation._materials,
        checkpoint=checkpoint,
    )
    if OperationGuard
    else None
)


def guard():
    calls["guard"] += 1
    if boundary:
        boundary()
    else:
        checkpoint()
        assert evaluation._materials(command).as_payload() == materials.as_payload()


if boundary:
    guard.checkpoint = checkpoint


def handler(request):
    if request.url.path.endswith("/input_tokens"):
        calls["count"] += 1
        return httpx2.Response(200, json={"input_tokens": 100})
    calls["submit"] += 1
    native = json.loads(request.content)
    return httpx2.Response(
        200,
        json={
            "id": "resp_probe",
            "model": native["model"],
            "metadata": native["metadata"],
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "probe_read",
                    "name": "read_source",
                    "status": "completed",
                    "arguments": json.dumps(
                        {"source_id": source_id, "start": 0, "end": 100}
                    ),
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    )


store.metrics.clear()
event.listen(repo.database.engine, "before_cursor_execute", capture)
start = time.perf_counter()
with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
    gateway = ModelGateway(
        j,
        lambda _: OpenAIProvider("test-key-absent-from-content", http_client=client),
        source_corpus=corpus,
    )
    try:
        gateway.invoke(
            root,
            profile,
            limits=command.limits,
            totals={"input": 0, "output": 0},
            guard=guard,
        )
    except ProbeStop:
        pass
report = {
    "measurement_contract": "local-model-tool-cycle/1",
    "provider_is_mock": True,
    "source_is_isolated_clone": True,
    "excludes": ["corpus startup", "search/indexing", "provider latency"],
    "corpus_documents": len(corpus.documents),
    "full_cycle": {
        "seconds": round(time.perf_counter() - start, 4),
        "sql": dict(sql),
        "native": dict(calls),
        "objects": dict(store.metrics),
        "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if platform.system() == "Darwin" else 1024),
    },
}
if phases:
    report["full_cycle"]["breakdown"] = {
        "phases": phases.most_common(),
        "top_sql": statements.most_common(25),
        "phase_sql": [
            {"phase": p, "sql": q, "count": n}
            for (p, q), n in phase_sql.most_common(40)
        ],
    }
print(json.dumps(report), flush=True)
# One physical-turn recovery evidence operation over the retained real history.
turn = model_turns.rows(j, original)[-1]
sql.clear()
store.metrics.clear()
start = time.perf_counter()
view = TurnEvidence(j, original, turn)
selected = view.events(view.turn)
report["recovery"] = {
    "history_turns": len(model_turns.rows(j, original)),
    "selected_events": len(selected),
    "seconds": round(time.perf_counter() - start, 4),
    "sql": dict(sql),
    "objects": dict(store.metrics),
}
Path(os.environ["PODGEN_PROBE_REPORT"]).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2), flush=True)
