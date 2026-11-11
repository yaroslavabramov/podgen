"""Evaluator export: all attempts, full candidates and real request/response bodies."""

import json
import os
from pathlib import Path
from uuid import uuid4

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict

from . import author_material
from .contracts import STAGES
from .journal import EvaluationJournal
from .usage import refused_without_generation, unsent_without_generation


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
    if invocation["attempt_kind"] in ("total_input_retry", "technical_retry"):
        write_json(base / "request-record.json", request)
    _write(
        directory / base / "request.json",
        journal.read_blob(request_ref, BlobRef.model_validate(request["request_blob"])),
    )
    events = []
    for index, event in enumerate(journal.events(invocation)):
        ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
        record = journal.read(ref)
        events.append(dict(record_ref=ref.model_dump(mode="json"), **record))
        for field in ("response_blob", "wire_request_blob", "wire_response_blob"):
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
    source_events = [
        record for record in events if record["event_kind"] in ("source_tool_call", "source_tool_result")
    ]
    if source_events:
        write_json(base / "source-tools.json", source_events)
    search_events = [record for record in events if record["event_kind"] == "source_search_diagnostics"]
    if search_events:
        write_json(base / "source-search.json", search_events)
    from .source_tool_contract import applies, from_manifest

    if invocation["operation_kind"] == "role" and applies(
        invocation["stage"], from_manifest(journal.manifest(invocation["run_id"]))
    ):
        from .model_turns import rows

        for turn in rows(journal, invocation):
            prefix = base / "turns" / f"{turn['turn_index']:03d}"
            write_json(prefix / "state.json", _projection(turn))
            write_json(
                prefix / "events.json",
                [
                    record
                    for record in events
                    if record["data"].get("source_turn_index") == turn["turn_index"]
                ],
            )
            ref = RuntimeRef(record_kind="tool_execution", record_id=turn["request_record_id"])
            stored = journal.read(ref)
            _write(
                directory / prefix / "request.json",
                journal.read_blob(ref, BlobRef.model_validate(stored["request_blob"])),
            )


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
        additional_conditions=[],
        input_tokens=0,
        output_tokens_known=0,
        unknown_usage_invocations=[],
    )

    def write_json(path, value):
        _write(directory / path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())

    write_json(Path("manifest.json"), manifest)
    if manifest.get("planning_continuation"):
        from .planning_continuation.export import export as export_continuation

        export_continuation(journal, manifest, write_json)
        result["planning_continuation"] = manifest["planning_continuation"]
    from .total_input_recovery import effective_ceiling

    ceiling = effective_ceiling(journal, run_id)
    if ceiling != manifest["command"]["limits"]["max_total_input_tokens"]:
        result["authorized_max_total_input_tokens"] = ceiling
    manifest_ref = RuntimeRef(record_kind="context_assembly", record_id=row["manifest_id"])
    _write(
        directory / "inputs.json",
        journal.read_blob(manifest_ref, BlobRef.model_validate(manifest["materials_blob"])),
    )
    from .reading_workflow import VERSION, base_stage

    for stage in manifest.get("stages", STAGES):
        attempts = journal.attempts(run_id, stage)
        if not attempts:
            continue
        for invocation in attempts:
            if invocation["attempt_kind"] == "plan_target_repair":
                result["additional_conditions"].append(
                    dict(
                        kind="plan-target-repair-v1",
                        stage=stage,
                        invocation_id=str(invocation["invocation_id"]),
                        source_invocation_id=str(invocation["parent_invocation_id"]),
                    )
                )
            base = Path("archive") / stage
            if invocation["attempt_index"]:
                base /= f"attempt-{invocation['attempt_index']}"
            _export_attempt(journal, invocation, base, write_json, directory)
            from .source_tool_contract import applies, from_manifest

            if invocation["operation_kind"] == "role" and applies(stage, from_manifest(manifest)):
                from .model_turns import charged_rows

                # Known physical output is shown even if a different turn has unknown usage.
                turns = charged_rows(journal, invocation)
                result["input_tokens"] += invocation["input_tokens"] or 0
                result["output_tokens_known"] += sum(turn["output_tokens"] or 0 for turn in turns)
                result["unknown_usage_invocations"].extend(
                    str(turn["invocation_id"]) for turn in turns if turn["output_tokens"] is None
                )
                continue
            unsent = unsent_without_generation(journal, invocation)
            if not unsent:
                result["input_tokens"] += invocation["input_tokens"] or 0
            if invocation["output_tokens"] is None and not (
                unsent or refused_without_generation(journal, invocation)
            ):
                result["unknown_usage_invocations"].append(str(invocation["invocation_id"]))
            else:
                result["output_tokens_known"] += invocation["output_tokens"] or 0
        selected = attempts[-1]
        result["invocations"][stage] = dict(
            **_projection(selected), attempts=[_projection(item) for item in attempts]
        )
        if selected["output_record_id"]:
            output = journal.output(selected)
            if output.get("contract") == author_material.CONTRACT:
                stopped = author_material.result(output)
                write_json(Path("material-request.json"), output)
                if stopped["last_script_ref"] is not None:
                    write_json(Path("last-script-ref.json"), stopped["last_script_ref"])
                need = output["material_request"]
                review = f"{need['question']}\n\n{need['why_needed']}\n"
                _write(directory / "review-result.md", review.encode())
                result["writer_material_request"] = stopped
                result["review_status"] = stopped["review_status"]
                continue
            write_json(Path(stage + ".json"), output)
            if base_stage(stage) in ("draft", "revise"):
                _write(
                    directory / (stage + ".txt"),
                    (
                        "\n\n".join(u["speaker"] + ": " + u["text"] for u in output["output"]["utterances"])
                        + "\n"
                    ).encode(),
                )
    if manifest.get("input_contract") == "editorial-role-packets/1":
        from .editorial_input_export import export_inputs

        export_inputs(
            repo,
            project,
            manifest,
            write_json=write_json,
            write_bytes=lambda name, data: _write(directory / name, data),
        )
        from .finalization import final_ref, finalization_view, read_finalization

        final = read_finalization(journal, run_id)
        if final is not None:
            result["finalization"] = finalization_view(final, final_ref(run_id))
            for name in ("final", "revise"):
                write_json(Path(name + ".json"), final)
                _write(
                    directory / (name + ".txt"),
                    (
                        "\n\n".join(u["speaker"] + ": " + u["text"] for u in final["output"]["utterances"])
                        + "\n"
                    ).encode(),
                )
    if (
        row["status"] != "plan_ready"
        and manifest.get("review_workflow") == VERSION
        and "writer_material_request" not in result
    ):
        from .reading_export import export_readings

        result.update(
            export_readings(
                journal,
                run_id,
                manifest,
                write_json,
                lambda path, value: _write(directory / path, value.encode()),
            )
        )
    if manifest.get("plan_mode") == "own-plan":
        from .planning_export import export_planning

        readable, proof = export_planning(journal, run_id, language=manifest["output_language"])
        if readable is not None:
            _write(directory / "generated-plan.md", readable.encode("utf-8"))
        if proof is not None:
            write_json(Path("planning-clearance.json"), proof)
            if row["status"] == "plan_ready":
                from .planning import clearance_ref

                result.update(
                    accepted_plan_ref=proof["candidate_plan_ref"],
                    accepted_plan_hash=proof["candidate_plan_hash"],
                    plan_review_ref=proof["plan_review_ref"],
                    planning_clearance_ref=clearance_ref(run_id).model_dump(mode="json"),
                )
    write_json(Path("run.json"), result)
    return result
