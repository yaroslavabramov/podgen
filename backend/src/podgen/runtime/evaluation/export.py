"""Evaluator export: all attempts, full candidates and real request/response bodies."""

import json
import os
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict

from .contracts import STAGES
from .journal import EvaluationJournal
from .usage import refused_without_generation


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _claim_directory(directory, project, run_id):
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".podgen-writing-export.json"
    expected = dict(contract="writing-evaluation-export-v1", project_id=str(project), run_id=str(run_id))
    if marker.is_symlink() or directory.is_symlink():
        raise Conflict("unsafe_export_directory")
    if marker.exists():
        try:
            actual = json.loads(marker.read_text())
        except (ValueError, OSError):
            raise Conflict("invalid_export_directory_marker") from None
        if actual != expected:
            raise Conflict("export_directory_belongs_to_another_run")
    else:
        if any(directory.iterdir()):
            raise Conflict("export_directory_not_empty")
        try:
            with marker.open("x", encoding="utf-8") as output:
                json.dump(expected, output)
        except FileExistsError:
            raise Conflict("export_directory_claim_race") from None
    if any(path.is_symlink() for path in directory.rglob("*")):
        raise Conflict("unsafe_export_symlink")


def _projection(invocation):
    return {
        key: str(value) if key.endswith("_id") and value is not None else value
        for key, value in invocation.items()
    }


def _export_attempt(journal, invocation, base, write_json, directory):
    request_ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
    request = journal.read(request_ref)
    _write(
        directory / base / "request.json",
        journal.read_blob(request_ref, BlobRef.model_validate(request["request_blob"])),
    )
    events = []
    for index, event in enumerate(journal.events(invocation)):
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(ref)
        events.append(dict(record_ref=ref.model_dump(mode="json"), **record))
        for field in ("response_blob", "wire_request_blob"):
            if record.get(field):
                _write(
                    directory / base / f"{index:03d}-{event['event_kind']}-{field}.json",
                    journal.read_blob(ref, BlobRef.model_validate(record[field])),
                )
        if event["event_kind"] == "count_intent" and record["data"].get("request_blob"):
            _write(
                directory / base / f"{index:03d}-count-request.json",
                journal.read_blob(ref, BlobRef.model_validate(record["data"]["request_blob"])),
            )
    write_json(base / "events.json", events)


def export_run(repo, project, run_id, directory):
    journal = EvaluationJournal(repo, project)
    row = journal.run(run_id)
    if row is None:
        raise Conflict("evaluation_run_missing")
    directory = Path(directory)
    _claim_directory(directory, project, run_id)
    manifest = journal.manifest(run_id)
    result = dict(
        run_id=str(run_id),
        case_id=manifest["command"]["case_id"],
        status=row["status"],
        failure_code=row["failure_code"],
        semantic_acceptance=False,
        production_ready=False,
        invocations={},
        input_tokens=0,
        output_tokens_known=0,
        unknown_usage_invocations=[],
    )

    def write_json(path, value):
        _write(directory / path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())

    write_json(Path("manifest.json"), manifest)
    manifest_ref = RuntimeRef(record_kind="context_assembly", record_id=row["manifest_id"])
    _write(
        directory / "inputs.json",
        journal.read_blob(manifest_ref, BlobRef.model_validate(manifest["materials_blob"])),
    )
    for stage in manifest.get("stages", STAGES):
        attempts = journal.attempts(run_id, stage)
        if not attempts:
            continue
        for invocation in attempts:
            base = Path("archive") / stage
            if invocation["attempt_index"]:
                base /= f"attempt-{invocation['attempt_index']}"
            _export_attempt(journal, invocation, base, write_json, directory)
            result["input_tokens"] += invocation["input_tokens"] or 0
            if invocation["output_tokens"] is None and not refused_without_generation(journal, invocation):
                result["unknown_usage_invocations"].append(str(invocation["invocation_id"]))
            else:
                result["output_tokens_known"] += invocation["output_tokens"] or 0
        selected = attempts[-1]
        result["invocations"][stage] = dict(
            **_projection(selected), attempts=[_projection(item) for item in attempts]
        )
        if selected["output_record_id"]:
            output = journal.output(selected)
            write_json(Path(stage + ".json"), output)
            if stage in ("draft", "revise"):
                _write(
                    directory / (stage + ".txt"),
                    (
                        "\n\n".join(u["speaker"] + ": " + u["text"] for u in output["output"]["utterances"])
                        + "\n"
                    ).encode(),
                )
    if manifest.get("plan_mode") == "own-plan":
        from .planning_export import export_planning

        readable, proof = export_planning(journal, run_id, language=manifest["output_language"])
        if readable is not None:
            _write(directory / "generated-plan.md", readable.encode("utf-8"))
        if proof is not None:
            write_json(Path("planning-clearance.json"), proof)
    write_json(Path("run.json"), result)
    return result
