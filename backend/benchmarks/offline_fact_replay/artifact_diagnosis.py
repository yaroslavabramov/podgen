"""Read-only attribution of repeated artifact metadata and semantic payload loads."""

import cProfile
import json
import os
import pstats
import time
from collections import Counter
from pathlib import Path
from uuid import UUID

from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import Database, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.read_authorization import authorized_read
from podgen.persistence.repository import Repository
from podgen.persistence.runtime_records import _validate_inputs
from podgen.runtime.evaluation.journal import EvaluationJournal

state = json.loads(Path(os.environ["PODGEN_REPLAY_STATE"]).read_text())
database = Database(state["database_url"])
repo = Repository(
    database, FileObjectStore(Path(state["objects"])), UUID(state["actor"]), purpose="evaluation"
)
journal = EvaluationJournal(repo, UUID(state["project"]))
run_id = UUID("a07cc6d1-c399-5979-a698-084ece565c47")
root = journal.invocation(run_id, "fact-review")
manifest = journal.manifest(run_id)
root_ref = RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"])
saved = next(row for row in journal.events(root) if row["event_kind"] == "source_context_plan")
payload = journal.read(RuntimeRef(record_kind="tool_execution", record_id=saved["record_id"]))
probes, current = [], None


def capture(conn, cursor, sql, parameters, context, many):
    if current is None:
        return
    current["sql_calls"] += 1
    if "AS access_graph" in sql:
        current["frontiers"].append([str(value) for value in parameters["ids"]])
        current["selects_semantic_payload"].append("payload" in [column[0] for column in cursor.description])


event.listen(database.engine, "after_cursor_execute", capture)
try:
    profiler = cProfile.Profile()
    with journal.runtime_graph():
        repo.authorize_runtime(journal.project, root_ref)
        profiler.enable()
        cpu_started, wall_started = time.process_time(), time.perf_counter()
        for index in range(3):
            current = dict(
                operation=f"event-input-validation-{index}",
                sql_calls=0,
                frontiers=[],
                selects_semantic_payload=[],
            )
            with database.read_transaction() as tx, authorized_read(repo, tx):
                journal.authorize(tx)
                _validate_inputs(repo, tx, journal.project, payload, "evaluator")
            probes.append(current)
            current = None
        validation_cpu = time.process_time() - cpu_started
        validation_wall = time.perf_counter() - wall_started
        profiler.disable()
    generation = Repository(database, repo.objects, repo.actor_id, purpose="generation")
    references = [RuntimeRef.model_validate(manifest["command"]["author_ref"])]
    if manifest["command"].get("fixed_plan_input_ref"):
        references.append(RuntimeRef.model_validate(manifest["command"]["fixed_plan_input_ref"]))
    current = dict(
        operation="external-boundary-generation-guard", sql_calls=0, frontiers=[], selects_semantic_payload=[]
    )
    generation.authorize_runtime_many(journal.project, references)
    probes.append(current)
    current = None
    frequency = Counter(value for probe in probes for frontier in probe["frontiers"] for value in frontier)
    with database.read_transaction() as tx:
        sizes = [
            dict(row)
            for row in query(
                tx,
                """SELECT revision_id,kind,
            octet_length(payload::text) AS stored_payload_bytes,
            octet_length(input_refs::text) AS input_ref_bytes
            FROM artifact_revisions WHERE revision_id=ANY(:ids)""",
                ids=[UUID(value) for value in frequency],
            ).mappings()
        ]
    payload_frequency = Counter(
        identity
        for probe in probes
        for frontier, selected in zip(probe["frontiers"], probe["selects_semantic_payload"], strict=True)
        if selected
        for identity in frontier
    )
    for row in sizes:
        row["revision_id"] = str(row["revision_id"])
        row["observed_loads"] = frequency[row["revision_id"]]
        row["observed_payload_bytes"] = payload_frequency[row["revision_id"]] * row["stored_payload_bytes"]
        row["payload_bytes_avoided"] = (
            row["observed_loads"] * row["stored_payload_bytes"] - row["observed_payload_bytes"]
        )
    for probe in probes:
        probe["payload_bytes_loaded"] = sum(
            row["stored_payload_bytes"]
            for frontier, selects_payload in zip(
                probe["frontiers"], probe["selects_semantic_payload"], strict=True
            )
            if selects_payload
            for identity in frontier
            for row in sizes
            if row["revision_id"] == identity
        )
    output = Path(os.environ["PODGEN_REPLAY_OUT"])
    output.mkdir(parents=True, exist_ok=True)
    statistics = pstats.Stats(profiler).stats
    profile_rows = [
        dict(
            function=f"{Path(key[0]).name}:{key[1]}:{key[2]}",
            calls=value[1],
            self_seconds=value[2],
            cumulative_seconds=value[3],
        )
        for key, value in statistics.items()
    ]
    report = dict(
        validation_cpu_seconds=validation_cpu,
        validation_wall_seconds=validation_wall,
        cumulative_profile=sorted(profile_rows, key=lambda value: value["cumulative_seconds"], reverse=True)[
            :40
        ],
        probes=probes,
        artifacts=sizes,
        scope="Three read-only validations of one real saved event, plus one real generation guard; no publication or provider calls.",
        byte_definition="Stored payload::text UTF-8 sizes are measured separately after authorization. Observed bytes count only frontiers whose cursor returned a payload column; metadata and SQL protocol bytes are excluded.",
        byte_derivation="observed_payload_bytes counts only recorded frontiers whose cursor returned a payload column; avoided bytes = metadata-load count × stored payload bytes minus observed bytes. Derived from the same probe, no rerun.",
    )
    (output / "artifact-diagnosis.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
finally:
    database.engine.dispose()
