"""Bounded event/body reads and a linear archive pass, preserving historical bytes."""

import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.private_journal.archive_pages import event_pages, turn_pages

from .export_files import json_array, projection, staged_files, write

BATCH = 64


def read_many(journal, references):
    return journal.read_many(references)


def export_attempt(journal, invocation, base, write_json, directory):
    request_ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
    with journal.read_operation():
        request = journal.read(request_ref)
        raw = journal.read_blob(request_ref, BlobRef.model_validate(request["request_blob"]))
        if invocation["attempt_kind"] in ("total_input_retry", "technical_retry"):
            write_json(base / "request-record.json", request)
        write(directory / base / "request.json", raw)
    with TemporaryDirectory(prefix=".turn-events-", dir=directory) as spool:
        search_usage = _events(journal, invocation, directory / base, Path(spool))
        _turns(journal, invocation, base, directory, Path(spool))
    return search_usage


def _events(journal, invocation, directory, spool):
    search_usage = dict(tool_calls=0, search_calls=0)
    with ExitStack() as outputs:
        all_events = outputs.enter_context(json_array(directory / "events.json"))
        selected = {}
        index = 0
        for page in event_pages(journal, invocation):
            references = [
                RuntimeRef(record_kind="tool_execution", record_id=row["record_id"]) for row in page
            ]
            with journal.read_operation():
                records = read_many(journal, references)
                blobs = []
                for event, reference, record in zip(page, references, records, strict=True):
                    value = dict(record_ref=reference.model_dump(mode="json"), **record)
                    all_events(value)
                    kind = event["event_kind"]
                    if kind == "source_web_search":
                        for key in search_usage:
                            search_usage[key] += record["data"][key]
                    name = {
                        "source_tool_call": "source-tools.json",
                        "source_tool_result": "source-tools.json",
                        "source_search_diagnostics": "source-search.json",
                        "source_web_search": "web-search.json",
                    }.get(kind)
                    if name:
                        if name not in selected:
                            selected[name] = outputs.enter_context(json_array(directory / name))
                        selected[name](value)
                    turn = record["data"].get("source_turn_index")
                    if isinstance(turn, (int, float)) and turn == int(turn):
                        with (spool / f"{int(turn)}.jsonl").open("a", encoding="utf-8") as target:
                            target.write(json.dumps(value, ensure_ascii=False) + "\n")
                    for field in ("response_blob", "wire_request_blob", "wire_response_blob"):
                        if record.get(field):
                            blobs.append(
                                (
                                    directory / f"{index:03d}-{kind}-{field}.json",
                                    reference,
                                    BlobRef.model_validate(record[field]),
                                )
                            )
                    if kind == "count_intent" and record["data"].get("request_blob"):
                        blobs.append(
                            (
                                directory / f"{index:03d}-count-request.json",
                                reference,
                                BlobRef.model_validate(record["data"]["request_blob"]),
                            )
                        )
                    index += 1
                # Consume the metadata batch before storage I/O. A remote read
                # releases its snapshot; reauthorize the entire batch afterward,
                # including records with no blob of their own.
                if blobs:
                    with staged_files(directory) as staged:
                        for path, reference, blob in blobs:
                            write(staged / path.relative_to(directory), journal.read_blob(reference, blob))
                        journal.authorize_many(references)

    return search_usage


def _turns(journal, invocation, base, directory, spool):
    from .source_tool_contract import applies, from_manifest

    if invocation["operation_kind"] != "role" or not applies(
        invocation["stage"], from_manifest(journal.manifest(invocation["run_id"]))
    ):
        return
    for turns in turn_pages(journal, invocation):
        references = [
            RuntimeRef(record_kind="tool_execution", record_id=turn["request_record_id"]) for turn in turns
        ]
        with journal.read_operation():
            records = read_many(journal, references)
            with staged_files(directory / base / "turns") as staged:
                for turn, reference, stored in zip(turns, references, records, strict=True):
                    prefix = staged / f"{turn['turn_index']:03d}"
                    write(
                        prefix / "state.json",
                        (json.dumps(projection(turn), ensure_ascii=False, indent=2) + "\n").encode(),
                    )
                    with json_array(prefix / "events.json") as append:
                        source = spool / f"{turn['turn_index']}.jsonl"
                        if source.exists():
                            with source.open(encoding="utf-8") as stream:
                                for line in stream:
                                    append(json.loads(line))
                    write(
                        prefix / "request.json",
                        journal.read_blob(reference, BlobRef.model_validate(stored["request_blob"])),
                    )
                journal.authorize_many(references)
