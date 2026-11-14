"""Bounded next-action work, one-time adoption and durable exchange recovery."""

import json
from collections import Counter
from copy import deepcopy

import pytest

from podgen.domain.errors import ContractError
from podgen.research.hosted.service import advance
from podgen.research.hosted.state import latest, status
from tests.hosted_research_support import Provider, ready, response


def observe(journal, monkeypatch):
    work = Counter()
    for method in ("get", "bytes", "head", "exchange", "adopt_head"):
        original = getattr(journal, method)

        def counted(*args, _method=method, _original=original, **kwargs):
            work[_method] += 1
            result = _original(*args, **kwargs)
            if _method == "bytes":
                work["blob_bytes_read"] += len(result)
            elif _method in ("get", "head") and result is not None:
                work["record_bytes_read"] += len(json.dumps(result).encode())
            return result

        monkeypatch.setattr(journal, method, counted)
    return work


@pytest.mark.parametrize("polls", [1, 20, 100])
def test_next_poll_and_status_do_not_read_history(monkeypatch, polls):
    journal, _ = ready(monkeypatch)
    provider = Provider(*(response(steps=[dict(content="old snapshot " * 200)]) for _ in range(polls + 2)))
    advance(journal, provider)
    for _ in range(polls):
        advance(journal, provider)
    work = observe(journal, monkeypatch)
    old = {key for key in journal.records if key.startswith("exchange:")}
    original = journal.get

    def no_history(key):
        assert key not in old, "next action must not read a previous exchange"
        return original(key)

    monkeypatch.setattr(journal, "get", no_history)
    result = advance(journal, provider)
    assert result["state"] == "in_progress" and result["submissions"] == 1
    assert work["head"] == 1 and work["exchange"] == 2 and work["bytes"] == 1
    assert work["get"] == 6 and work["adopt_head"] == 0
    # The only blob read is the fixed original request, never an old HTTP body.
    assert work["blob_bytes_read"] == journal.get("manifest")["request_blob"]["byte_length"]
    work.clear()
    assert status(journal)["state"] == "in_progress"
    assert work == Counter(head=1, get=2, record_bytes_read=work["record_bytes_read"])


def test_legacy_adoption_once_preserves_archive_and_job_after_failed_get(monkeypatch):
    journal, _ = ready(monkeypatch)
    from podgen.research.hosted.transport import Response

    provider = Provider(response(), *(Response(503, {}, b"failed poll") for _ in range(20)), response())
    for _ in range(21):
        advance(journal, provider)
    archive = deepcopy(journal.records)
    blobs = deepcopy(journal.blobs)
    journal.projection = None  # Existing immutable archive, without a durable head.
    work = observe(journal, monkeypatch)
    assert status(journal)["provider_job_id"] == "job/one"
    assert work["adopt_head"] == 1 and work["bytes"] == 1
    assert journal.records == archive and journal.blobs == blobs
    work.clear()
    assert status(journal)["state"] == "in_progress"
    assert work["head"] == 1 and work["get"] == 2 and work["bytes"] == 0
    assert advance(journal, provider)["state"] == "in_progress"
    assert work["adopt_head"] == 0
    assert [kind for kind, _ in provider.calls].count("submit") == 1


@pytest.mark.parametrize("operation", ["submit", "get"])
def test_crash_after_intent_reopens_without_repeating_submit(monkeypatch, operation):
    journal, _ = ready(monkeypatch)
    provider = Provider(response(), RuntimeError("crash after GET intent"), response())
    if operation == "get":
        advance(journal, provider)
    else:
        provider = Provider(RuntimeError("crash after submit intent"))
    with pytest.raises(RuntimeError):
        advance(journal, provider)
    assert journal.head()["last_exchange"] == dict(
        key="exchange:2" if operation == "get" else "exchange:1",
        operation=operation,
        receipt=False,
    )
    assert advance(journal, provider)["state"] == ("in_progress" if operation == "get" else "outcome_unknown")
    assert [kind for kind, _ in provider.calls].count("submit") == 1


def test_different_job_is_durable_integrity_error_and_never_polled(monkeypatch):
    journal, _ = ready(monkeypatch)
    from podgen.research.hosted.transport import Response

    provider = Provider(response(), Response(200, {}, b'{"id":"another-job","status":"in_progress"}'))
    advance(journal, provider)
    with pytest.raises(ContractError, match="provider_job_changed"):
        advance(journal, provider)
    assert journal.get("exchange:2:receipt")
    assert journal.head()["current"]["id"] == "job/one"
    assert status(journal)["state"] == "needs_attention"
    with pytest.raises(ContractError, match="provider_job_changed"):
        advance(journal, provider)
    assert len(provider.calls) == 2


def test_completed_status_reads_no_raw_snapshots_or_history(monkeypatch):
    journal, _ = ready(monkeypatch)
    provider = Provider(
        response(
            "completed",
            steps=[dict(type="model_output", content=[dict(type="text", text="Complete report")])],
        )
    )
    advance(journal, provider)
    work = observe(journal, monkeypatch)
    for _ in range(20):
        assert status(journal)["state"] == "completed"
    assert work["head"] == 20 and work["get"] == 40 and work["bytes"] == 0
    assert advance(journal, provider)["state"] == "completed"
    assert work["bytes"] == 1  # Only manifest validation, not completed provider output.
    assert len(provider.calls) == 1


def test_completed_receipt_adoption_can_process_without_provider(monkeypatch):
    journal, _ = ready(monkeypatch)
    provider = Provider(response("completed", steps=[]))
    with pytest.raises(ContractError, match="without_report"):
        advance(journal, provider)
    journal.projection = None
    assert status(journal)["state"] == "result_processing_required"
    assert latest(journal)[1]["status"] == "completed"
    with pytest.raises(ContractError, match="without_report"):
        advance(journal, provider)
    assert len(provider.calls) == 1


def test_legacy_unknown_submit_remains_unknown_after_adoption(monkeypatch):
    journal, _ = ready(monkeypatch)
    provider = Provider(RuntimeError("lost acknowledgement"))
    with pytest.raises(RuntimeError):
        advance(journal, provider)
    journal.projection = None
    assert status(journal)["state"] == "outcome_unknown"
    assert journal.head()["last_exchange"]["receipt"] is False
    assert advance(journal, provider)["state"] == "outcome_unknown"
    assert len(provider.calls) == 1


def test_job_is_written_only_once_and_never_read_by_get(monkeypatch):
    journal, _ = ready(monkeypatch)
    writes = []
    original_put, original_get = journal.put, journal.get

    def put(key, value):
        writes.append(key)
        return original_put(key, value)

    def get(key):
        assert key != "job", "normal polls must use the durable job binding"
        return original_get(key)

    monkeypatch.setattr(journal, "put", put)
    monkeypatch.setattr(journal, "get", get)
    provider = Provider(*(response() for _ in range(21)))
    for _ in range(21):
        advance(journal, provider)
    assert writes.count("job") == 1
    assert journal.head()["job_recorded"] is True


@pytest.mark.parametrize("completed", [False, True])
def test_legacy_missing_job_is_repaired_once_before_continuing(monkeypatch, completed):
    journal, _ = ready(monkeypatch)
    provider = Provider(
        response(
            "completed" if completed else "in_progress",
            steps=[dict(type="model_output", content=[dict(type="text", text="Full report")])],
        ),
        response(),
        response(),
    )
    advance(journal, provider)
    journal.records.pop("job")
    journal.records.pop("result", None)
    journal.projection = None
    status(journal)
    assert journal.head()["job_recorded"] is False
    original_put, original_get = journal.put, journal.get
    repairs = []

    def put(key, value):
        if key == "job":
            repairs.append(value)
        return original_put(key, value)

    def get(key):
        assert key != "job", "legacy repair must rely on its adopted binding"
        return original_get(key)

    monkeypatch.setattr(journal, "put", put)
    monkeypatch.setattr(journal, "get", get)
    for _ in range(2):
        assert advance(journal, provider)["state"] == ("completed" if completed else "in_progress")
    assert len(repairs) == 1 and journal.head()["job_recorded"] is True
    assert [kind for kind, _ in provider.calls].count("submit") == 1
