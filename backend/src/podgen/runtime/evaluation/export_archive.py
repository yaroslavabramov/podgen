"""Bounded event/body reads and a linear archive pass, preserving historical bytes."""

import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.database import query

from .export_files import json_array, projection, write

BATCH = 64


def _pages(journal, invocation, table, identity_field, order):
    params = dict(p=journal.project, identity=invocation["invocation_id"])
    selector = f"project_id=:p AND {identity_field}=:identity"
    with journal.repo.database.transaction() as tx:
        journal.authorize(tx)
        end = query(tx, f"SELECT max({order}) FROM {table} WHERE {selector}", **params).scalar()
    cursor = -1
    while end is not None and cursor < end:
        with journal.repo.database.transaction() as tx:
            journal.authorize(tx)
            page = [
                dict(row)
                for row in query(
                    tx,
                    f"SELECT * FROM {table} WHERE {selector} AND {order}>:cursor "
                    f"AND {order}<=:end ORDER BY {order} LIMIT :batch",
                    **params,
                    cursor=cursor,
                    end=end,
                    batch=BATCH,
                ).mappings()
            ]
        if not page:
            break
        cursor = page[-1][order]
        yield page


def event_pages(journal, invocation):
    return _pages(journal, invocation, "evaluation_invocation_events", "invocation_id", "sequence")


def turn_pages(journal, invocation):
    return _pages(journal, invocation, "evaluation_model_turns", "root_invocation_id", "turn_index")


def read_many(journal, references):
    return journal.repo.get_runtime_many(journal.project, references)


def export_attempt(journal, invocation, base, write_json, directory):
    request_ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
    with journal.read_operation():
        request = journal.read(request_ref)
        if invocation["attempt_kind"] in ("total_input_retry", "technical_retry"):
            write_json(base / "request-record.json", request)
        write(
            directory / base / "request.json",
            journal.read_blob(request_ref, BlobRef.model_validate(request["request_blob"])),
        )
    with TemporaryDirectory(prefix=".turn-events-", dir=directory) as spool:
        _events(journal, invocation, directory / base, Path(spool))
        _turns(journal, invocation, base, write_json, directory, Path(spool))


def _events(journal, invocation, directory, spool):
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
                    name = {
                        "source_tool_call": "source-tools.json",
                        "source_tool_result": "source-tools.json",
                        "source_search_diagnostics": "source-search.json",
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
                for path, reference, blob in blobs:
                    write(path, journal.read_blob(reference, blob))
                if blobs:
                    journal.repo.authorize_runtime_many(journal.project, references)


def _turns(journal, invocation, base, write_json, directory, spool):
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
            blobs = []
            for turn, reference, stored in zip(turns, references, records, strict=True):
                prefix = base / "turns" / f"{turn['turn_index']:03d}"
                write_json(prefix / "state.json", projection(turn))
                with json_array(directory / prefix / "events.json") as append:
                    source = spool / f"{turn['turn_index']}.jsonl"
                    if source.exists():
                        with source.open(encoding="utf-8") as stream:
                            for line in stream:
                                append(json.loads(line))
                blobs.append(
                    (
                        directory / prefix / "request.json",
                        reference,
                        BlobRef.model_validate(stored["request_blob"]),
                    )
                )
            for path, reference, blob in blobs:
                write(path, journal.read_blob(reference, blob))
            journal.repo.authorize_runtime_many(journal.project, references)
