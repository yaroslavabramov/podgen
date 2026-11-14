"""Exact archive arrays and bounded reads, including out-of-order physical turn events."""

import json
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.runtime.evaluation import export_archive
from podgen.runtime.evaluation.export_files import json_array


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


@pytest.mark.parametrize(
    "values", [[], [{}], [None, True, ["a", "ё 😀\r\n"], {"state": "Full state. " * 1000}]]
)
def test_streaming_arrays_keep_exact_historical_json_bytes(tmp_path, values):
    path = tmp_path / "events.json"
    with json_array(path) as append:
        for value in values:
            append(value)
    assert path.read_bytes() == encoded(values)


def test_failed_array_does_not_replace_previous_complete_file(tmp_path):
    path = tmp_path / "events.json"
    path.write_bytes(b"previous")
    with pytest.raises(ValueError):
        with json_array(path) as append:
            append({"partial": True})
            raise ValueError("read denied")
    assert path.read_bytes() == b"previous"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["events.json"]


def test_archive_visits_each_event_once_and_batches_body_reads(tmp_path, monkeypatch):
    records = {}
    events = []
    expected = []
    calls = []
    for i in range(130):
        ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        kind = (
            "source_tool_result"
            if i % 3 == 0
            else "source_search_diagnostics"
            if i % 3 == 1
            else "provider_response"
        )
        record = dict(
            event_kind=kind,
            data=dict(source_turn_index=i % 2, full_state="Exact retained ёжик 😀\r\n", result=dict(ok=True)),
        )
        records[ref.record_id] = record
        events.append(dict(record_id=ref.record_id, event_kind=kind))
        expected.append(dict(record_ref=ref.model_dump(mode="json"), **record))

    class Journal:
        read_operation = staticmethod(nullcontext)

        def read_many(self, references):
            assert len(references) <= 64
            calls.append(len(references))
            return [records[ref.record_id] for ref in references]

    journal = Journal()
    monkeypatch.setattr(
        export_archive, "event_pages", lambda *a: (events[i : i + 64] for i in range(0, len(events), 64))
    )
    spool = tmp_path / "spool"
    spool.mkdir()
    directory = tmp_path / "archive"
    export_archive._events(journal, {}, directory, spool)
    assert calls == [64, 64, 2]
    assert (directory / "events.json").read_bytes() == encoded(expected)
    assert (directory / "source-tools.json").read_bytes() == encoded(
        [r for r in expected if r["event_kind"] == "source_tool_result"]
    )
    assert (directory / "source-search.json").read_bytes() == encoded(
        [r for r in expected if r["event_kind"] == "source_search_diagnostics"]
    )
    for index in (0, 1):
        assert [json.loads(line) for line in (spool / f"{index}.jsonl").read_text().splitlines()] == [
            r for r in expected if r["data"]["source_turn_index"] == index
        ]


def test_archive_empty_history_writes_empty_array_without_optional_files(tmp_path, monkeypatch):
    journal = SimpleNamespace(project=uuid4())
    monkeypatch.setattr(export_archive, "event_pages", lambda *a: iter(()))
    export_archive._events(journal, {}, tmp_path / "archive", tmp_path)
    assert list((tmp_path / "archive").iterdir()) == [tmp_path / "archive/events.json"]
    assert (tmp_path / "archive/events.json").read_bytes() == b"[]\n"


def test_native_blob_and_count_request_bytes_remain_unchanged(tmp_path, monkeypatch):
    data = b'{"exact":"provider wire \\n bytes"}'
    blob = BlobRef(
        storage_key="test/blob",
        sha256=sha256(data).hexdigest(),
        byte_length=len(data),
        media_type="application/json",
    )
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    record = dict(
        event_kind="count_intent",
        data=dict(request_blob=blob.model_dump(mode="json")),
        response_blob=blob.model_dump(mode="json"),
        wire_request_blob=blob.model_dump(mode="json"),
        wire_response_blob=blob.model_dump(mode="json"),
    )
    journal = SimpleNamespace(
        project=uuid4(),
        read_many=lambda *a: [record],
        authorize_many=lambda *a: None,
        read_operation=nullcontext,
        read_blob=lambda *a: data,
    )
    monkeypatch.setattr(
        export_archive,
        "event_pages",
        lambda *a: iter([[dict(record_id=ref.record_id, event_kind="count_intent")]]),
    )
    directory = tmp_path / "archive"
    export_archive._events(journal, {}, directory, tmp_path)
    for name in ("response_blob", "wire_request_blob", "wire_response_blob"):
        assert (directory / f"000-count_intent-{name}.json").read_bytes() == data
    assert (directory / "000-count-request.json").read_bytes() == data


@pytest.mark.parametrize("denied", [False, True])
def test_turn_exports_preserve_filtered_event_order_and_empty_turns(tmp_path, monkeypatch, denied):
    payload = b'{"exact":"saved request"}'
    blob = BlobRef(
        storage_key="request",
        sha256=sha256(payload).hexdigest(),
        byte_length=len(payload),
        media_type="application/json",
    )
    turns = [dict(turn_index=i, request_record_id=uuid4(), invocation_id=uuid4()) for i in range(3)]
    selected = [
        dict(data=dict(source_turn_index=1), text="First"),
        dict(data=dict(source_turn_index=0), text="Middle"),
        dict(data=dict(source_turn_index=1), text="Last ё 😀"),
    ]
    spool = tmp_path / "spool"
    spool.mkdir()
    for i in range(2):
        (spool / f"{i}.jsonl").write_text(
            "".join(
                json.dumps(r, ensure_ascii=False) + "\n"
                for r in selected
                if r["data"]["source_turn_index"] == i
            )
        )
    manifest = dict(
        behavior_profile="production/1",
        source_tool_contract=dict(
            version="frozen-corpus-tools/1",
            definitions=[dict(type="function", name="read_source", parameters={})],
            max_model_turns=12,
            max_tool_calls=24,
        ),
    )

    def authorize_many(references):
        if denied:
            raise ValueError("access withdrawn during remote read")

    journal = SimpleNamespace(
        project=uuid4(),
        read_many=lambda refs: [dict(request_blob=blob.model_dump(mode="json")) for _ in refs],
        authorize_many=authorize_many,
        read_operation=nullcontext,
        read_blob=lambda *a: payload,
        manifest=lambda _: manifest,
    )
    monkeypatch.setattr(export_archive, "turn_pages", lambda *a: iter([turns]))

    previous = tmp_path / "archive/turns/001"
    if denied:
        previous.mkdir(parents=True)
        for name in ("state.json", "events.json", "request.json"):
            (previous / name).write_bytes(b"previous complete file")
    with pytest.raises(ValueError, match="access withdrawn") if denied else nullcontext():
        export_archive._turns(
            journal,
            dict(operation_kind="role", stage="fact-review", run_id=uuid4()),
            Path("archive"),
            tmp_path,
            spool,
        )
    if denied:
        assert sorted(p.name for p in (tmp_path / "archive/turns").iterdir()) == ["001"]
        assert sorted(p.name for p in previous.iterdir()) == ["events.json", "request.json", "state.json"]
        assert all(p.read_bytes() == b"previous complete file" for p in previous.iterdir())
        return
    for i in range(3):
        path = tmp_path / f"archive/turns/{i:03d}"
        assert (path / "request.json").read_bytes() == payload
        assert (path / "events.json").read_bytes() == encoded(
            [r for r in selected if r["data"]["source_turn_index"] == i]
        )
        assert json.loads((path / "state.json").read_text())["invocation_id"] == str(
            turns[i]["invocation_id"]
        )


def test_attempt_metadata_is_not_published_before_request_read_finishes(tmp_path):
    blob = BlobRef(
        storage_key="request",
        sha256=sha256(b"request").hexdigest(),
        byte_length=7,
        media_type="application/json",
    )
    written = []

    def denied(*args):
        raise ValueError("access withdrawn during remote read")

    journal = SimpleNamespace(
        read_operation=nullcontext,
        read=lambda _: dict(request_blob=blob.model_dump(mode="json")),
        read_blob=denied,
    )
    with pytest.raises(ValueError, match="access withdrawn"):
        export_archive.export_attempt(
            journal,
            dict(request_record_id=uuid4(), attempt_kind="technical_retry"),
            Path("archive"),
            lambda *args: written.append(args),
            tmp_path,
        )
    assert not written
    assert not list(tmp_path.iterdir())
