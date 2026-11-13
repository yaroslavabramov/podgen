"""Read-only SQL attribution of one saved final audit; never invokes a provider."""

import json
import os
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from uuid import UUID

from sqlalchemy import event

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.persistence.source_reading import read_sources
from podgen.persistence.text_source import VerifiedText
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.reading_audit import evidence, lifecycle
from podgen.runtime.evaluation.reading_audit.source import prepare
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.writing.model_references import context_references
from podgen.writing.reading_audit.registry import model
from podgen.writing.source_tools import SourceCorpus

state = json.loads(Path(os.environ["PODGEN_REPLAY_STATE"]).read_text())
database = Database(state["database_url"])
repo = Repository(
    database, FileObjectStore(Path(state["objects"])), UUID(state["actor"]), purpose="evaluation"
)
journal = EvaluationJournal(repo, UUID(state["project"]))
run_id = UUID("a07cc6d1-c399-5979-a698-084ece565c47")
manifest = journal.manifest(run_id)
command = WritingRunCommand.model_validate(manifest["command"])
materials = WritingEvaluation(repo, lambda _: None)._materials(command)
root = journal.invocation(run_id, "fact-review")
request = journal.read(RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]))
saved = journal.output(root)
script_ref = request["context"]["script_ref"]
script = journal.read(RuntimeRef.model_validate(script_ref))["output"]
audit = prepare(journal, run_id, "fact-review", manifest, materials, script, script_ref)
assert audit is not None
references = context_references(request["context"])
wire = saved["wire_output"]
parsed = model("fact-review").model_validate(references.translate(wire, inbound=True) if references else wire)
documents = [*materials.documents, *materials.runtime_admissions]
# The real final turn has already read these immutable local projections.
for document in documents:
    if isinstance(document.text, VerifiedText):
        document.text.verify()

stack, probes, current = [], [], None


def instrument(owner, name, label):
    original = getattr(owner, name)

    def wrapped(*args, **kwargs):
        stack.append(label)
        try:
            return original(*args, **kwargs)
        finally:
            stack.pop()

    setattr(owner, name, wrapped)


for owner, name in (
    (lifecycle, "current_catalog"),
    (lifecycle, "resolve"),
    (lifecycle, "_locator"),
    (lifecycle, "validate_retained"),
    (evidence, "_selectors"),
    (evidence, "validate_retained"),
    (SourceCorpus, "locate"),
    (VerifiedText, "__getitem__"),
):
    instrument(owner, name, owner.__name__ + "." + name)


def capture(conn, cursor, sql, parameters, context, many):
    if current is None:
        return
    executions = len(parameters) if many else 1
    tag = "/".join(stack) or "finalize"
    current["sql_executions"] += executions
    current["exclusive"][tag] += executions
    current["templates"][" ".join(sql.split())] += executions
    if "AS access_graph" in sql:
        current["artifact_frontiers"].append([str(value) for value in parameters["ids"]])
        assert "payload" not in [column[0] for column in cursor.description]


event.listen(database.engine, "after_cursor_execute", capture)
try:
    for scoped in (False, True, "production"):
        implementation = getattr(lifecycle, "_finalize", lifecycle.finalize)
        if scoped == "production":
            implementation = lifecycle.finalize
        current = dict(
            scoped=scoped, sql_executions=0, exclusive=Counter(), templates=Counter(), artifact_frontiers=[]
        )
        started = time.perf_counter()
        with ExitStack() as scopes:
            if scoped is True:
                scopes.enter_context(journal.read_operation())
                scopes.enter_context(read_sources(documents))
            normalized, proof = implementation(journal, root, parsed, audit, materials, script)
            assert normalized == saved["output"]
            assert proof == saved["reading_audit"]
        current["seconds"] = time.perf_counter() - started
        current["normalized_hash"] = digest(normalized)
        current["proof_hash"] = digest(proof)
        current["snapshot_count"] = sum(
            count for sql, count in current["templates"].items() if sql.startswith("SET TRANSACTION")
        )
        current["artifact_frontier_count"] = len(current["artifact_frontiers"])
        current["sql_templates"] = [
            dict(sql=sql, count=count) for sql, count in current.pop("templates").most_common()
        ]
        current["exclusive"] = dict(current["exclusive"])
        probes.append(current)
        current = None
finally:
    event.remove(database.engine, "after_cursor_execute", capture)
    database.engine.dispose()

output = Path(os.environ["PODGEN_REPLAY_OUT"])
output.mkdir(parents=True, exist_ok=True)
(output / "finalization-diagnosis.json").write_text(
    json.dumps(
        dict(
            run_id=str(run_id),
            operation="saved-final-audit-read-only",
            documents=len(documents),
            probes=probes,
            provider_calls=0,
            writes=0,
        ),
        indent=2,
    )
)
print(
    json.dumps(
        [
            {key: value for key, value in probe.items() if key not in ("artifact_frontiers", "sql_templates")}
            for probe in probes
        ]
    ),
    flush=True,
)
