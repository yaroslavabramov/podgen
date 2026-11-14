"""Qualify genuine v1/v2 archives through the public journal and gateway.

Restore each pinned source.dump into its own clean database and migrate to the
current head before running. Set PODGEN_PRIVATE_JOURNAL_LEGACY_FIXTURES and
PODGEN_PRIVATE_JOURNAL_LEGACY_DATABASE_URL_V1/V2. Each run consumes the restored
interrupted states; restore again before rerunning. No PostgreSQL lifecycle,
fixture fabrication, migrations or external provider calls happen in these tests.
"""

import base64
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.references import walk_values
from podgen.persistence.database import Database, query
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.research_context import ResearchContext
from podgen.runtime.models.research_history import ResearchHistory
from tests.private_journal_legacy_fixture import HISTORY_TABLES, WRITERS

UNKNOWN = "submission_acknowledgement_unknown|invocation_requires_new_attempt_or_reconciliation"


def _ordered(rows):
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))


def _archive_rows(repo, project):
    with repo.database.read_transaction() as tx:
        return {
            table: _ordered(
                list(
                    query(
                        tx,
                        f"SELECT row_to_json(t) FROM {table} t WHERE project_id=:p",
                        p=project,
                    ).scalars()
                )
            )
            for table in HISTORY_TABLES
        }


def _private_rows(case):
    with case.repo.database.read_transaction() as tx:
        return tuple(
            query(
                tx,
                f"SELECT count(*) FROM {table} WHERE project_id=:p AND run_id=:r",
                p=case.project,
                r=case.run_id,
            ).scalar_one()
            for table in ("execution_heads", "execution_entries")
        )


def _assert_old_content(case):
    # Construct another owner so process-local history cannot hide storage changes.
    journal = EvaluationJournal(case.repo, case.project)
    assert _archive_rows(case.repo, case.project) == case.before
    for saved in case.baseline["legacy_rows"]["runtime_records"]:
        reference = RuntimeRef(record_kind=saved["record_kind"], record_id=saved["record_id"])
        with journal.read_operation():
            assert journal.read(reference) == saved["payload"]
            for _, _, linked in walk_values(saved["payload"]):
                if isinstance(linked, BlobRef):
                    expected = base64.b64decode(case.baseline["object_bytes_base64"][linked.storage_key])
                    assert journal.read_blob(reference, linked) == expected
    root = journal.invocation(case.run_id, "draft")
    assert journal.request(root) == case.original_request
    assert _archive_rows(case.repo, case.project) == case.before


@pytest.fixture(params=("v1", "v2"))
def archive(request, tmp_path):
    directory = os.environ.get("PODGEN_PRIVATE_JOURNAL_LEGACY_FIXTURES")
    url = os.environ.get(f"PODGEN_PRIVATE_JOURNAL_LEGACY_DATABASE_URL_{request.param.upper()}")
    if not directory and not url:
        pytest.skip("Restore the genuine legacy fixture databases and set the legacy qualification variables")
    if not directory or not url:
        pytest.fail(
            "Legacy qualification requires the fixture directory and both version-specific database URLs"
        )
    database = Database(url)
    try:
        with database.read_transaction() as tx:
            assert all(
                query(tx, "SELECT to_regclass(:table)", table=table).scalar_one()
                for table in ("execution_heads", "execution_entries", "execution_objects")
            ), "Restore source.dump and migrate this database to v3 before qualification"
        source = Path(directory) / request.param
        objects = tmp_path / "objects"
        shutil.copytree(source / "objects", objects)
        yield request.param, source, database, objects
    finally:
        database.engine.dispose()


def _case(archive, state):
    writer, directory, database, objects = archive
    baseline = json.loads((directory / f"{state}.json").read_text())
    assert baseline["contract"] == "synthetic-legacy-journal-fixture/1"
    assert baseline["writer"] == writer and baseline["state"] == state
    assert baseline["source_commit"].startswith(WRITERS[writer])
    assert baseline["provider_calls"] == dict(count=1, submit=int(state != "counted"))
    project, run_id = UUID(baseline["project_id"]), UUID(baseline["run_id"])
    repo = Repository(database, FileObjectStore(objects), UUID(baseline["actor_id"]), purpose="evaluation")
    before = _archive_rows(repo, project)
    for table, expected in baseline["legacy_rows"].items():
        if not expected:
            assert not before[table]
            continue
        # A migration may add columns. Every historical column and row must remain exact.
        original_columns = tuple(expected[0])
        assert _ordered([{key: row[key] for key in original_columns} for row in before[table]]) == _ordered(
            expected
        )
    root_row = next(
        row
        for row in baseline["legacy_rows"]["evaluation_model_invocations"]
        if row["invocation_id"] == baseline["root_invocation_id"]
    )
    request = next(
        row["payload"]
        for row in baseline["legacy_rows"]["runtime_records"]
        if row["record_id"] == root_row["request_record_id"]
    )
    raw = base64.b64decode(baseline["object_bytes_base64"][request["request_blob"]["storage_key"]])
    case = SimpleNamespace(
        baseline=baseline,
        repo=repo,
        project=project,
        run_id=run_id,
        before=before,
        original_request=json.loads(raw),
        profile=SimpleNamespace(**baseline["profile"]),
        limits=SimpleNamespace(max_total_input_tokens=1000000, max_total_output_tokens=1000000),
    )
    assert _private_rows(case) == (0, 0), "Restore a fresh dump before each qualification run"
    _assert_old_content(case)
    return case


def _physical(case):
    journal = EvaluationJournal(case.repo, case.project)
    root = journal.invocation(case.run_id, "draft")
    row = next(
        row
        for row in model_turns.rows(journal, root)
        if str(row["invocation_id"]) == case.baseline["physical_invocation_id"]
    )
    turn = dict(root, **row)
    turn.update(attempt_index=0, output_record_id=None)
    return journal, root, model_turns.TurnJournal(journal, root, row["turn_index"]), turn


def _resume(case, provider, *, receipt=None):
    _, _, journal, turn = _physical(case)
    return ModelGateway(journal, lambda _: provider).invoke_once(
        turn,
        case.profile,
        limits=case.limits,
        totals=dict(input=0, output=0),
        guard=lambda: None,
        full_response=True,
        count_receipt=receipt,
    )


def _forbidden(*args, **kwargs):
    pytest.fail("Legacy resume repeated an external count, submit or retrieve")


def test_counted_legacy_receipt_is_reused_and_new_intent_is_only_v3(archive):
    case = _case(archive, "counted")
    journal, root, _, _ = _physical(case)
    sent = []

    class SentAfterDurableIntent(Exception):
        pass

    def submit(body, **options):
        sent.append((body, options))
        heads, entries = _private_rows(case)
        assert heads == 1 and entries > 0
        fresh = EvaluationJournal(case.repo, case.project)
        events = fresh.events(fresh.invocation(case.run_id, "draft"))
        assert sum(event["event_kind"] == "submit_intent" for event in events) == 1
        _assert_old_content(case)
        raise SentAfterDurableIntent()

    provider = SimpleNamespace(count=_forbidden, submit=submit, retrieve=_forbidden)
    context = ResearchContext(
        ModelGateway(journal, lambda _: provider),
        root,
        case.profile,
        case.original_request,
        ResearchHistory(journal, root),
        lambda: None,
    )
    assert context.measured(case.original_request, 0, "normal") == 100
    receipt = context.receipt(case.original_request)
    assert receipt is not None
    assert receipt.reference == RuntimeRef.model_validate(case.baseline["count_ref"])
    assert receipt.validate(journal, case.original_request, case.profile) == 100
    with pytest.raises(SentAfterDurableIntent):
        _resume(case, provider, receipt=receipt)
    assert len(sent) == 1
    assert sent[0][0] == case.original_request
    assert str(sent[0][1]["client_request_id"]) == case.baseline["physical_invocation_id"]
    with pytest.raises(RunFailure, match=UNKNOWN):
        _resume(case, SimpleNamespace(count=_forbidden, submit=_forbidden, retrieve=_forbidden))
    assert _physical(case)[3]["status"] in ("submitting", "outcome_unknown")
    _assert_old_content(case)


def test_unknown_legacy_submit_stays_uncertain_without_external_calls_or_old_writes(archive):
    case = _case(archive, "unknown")
    provider = SimpleNamespace(count=_forbidden, submit=_forbidden, retrieve=_forbidden)
    for _ in range(2):
        with pytest.raises(RunFailure, match=UNKNOWN):
            _resume(case, provider)
        assert _physical(case)[3]["status"] in ("submitting", "outcome_unknown")
        _assert_old_content(case)


@pytest.mark.parametrize("state", ["captured_tool", "captured_structured"])
def test_captured_legacy_response_continues_tools_without_provider_or_old_writes(archive, state):
    from podgen.runtime.evaluation.provider_evidence import register_structured
    from podgen.runtime.models.research_continuation import structured_call, structured_requests
    from podgen.runtime.models.tool_gateway import _calls, _execute

    case = _case(archive, state)
    response = _resume(case, SimpleNamespace(count=_forbidden, submit=_forbidden, retrieve=_forbidden))
    journal, root, _, _ = _physical(case)
    history = ResearchHistory(journal, root)
    if state == "captured_structured":
        requests = structured_requests(response, 0, history)
        calls = [structured_call(request, 0, index) for index, request in enumerate(requests)]
        decision = register_structured(
            journal, root, history.provider_response(0), requests, calls, 0, history
        )
        calls = [dict(call, decision_entry_id=str(decision)) for call in calls]
    else:
        calls = _calls(response)
    assert len(calls) == 1
    used = []

    def execute(name, arguments, **options):
        used.append((name, arguments))
        return dict(synthetic="complete saved tool result")

    corpus = SimpleNamespace(execute=execute)
    contract = dict(definitions=[dict(name=calls[0]["name"], parameters=dict(type="object"))])
    result = _execute(journal, root, corpus, contract, calls[0], 0, 0, lambda: None, history)
    assert result == dict(synthetic="complete saved tool result")
    assert len(used) == 1
    # A new owner must replay the captured tool result without executing it again.
    fresh = EvaluationJournal(case.repo, case.project)
    current = fresh.invocation(case.run_id, "draft")
    assert (
        _execute(
            fresh,
            current,
            SimpleNamespace(execute=_forbidden),
            contract,
            calls[0],
            0,
            0,
            lambda: None,
            ResearchHistory(fresh, current),
        )
        == result
    )
    assert _private_rows(case)[0] == 1
    _assert_old_content(case)
