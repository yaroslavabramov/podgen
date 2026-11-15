"""Ordinary export preserves paid physical usage across source-stage interruptions."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest

from podgen.runtime.evaluation import export as exporting
from podgen.runtime.evaluation import export_archive, model_turns
from podgen.runtime.evaluation.contracts import RunFailure
from podgen.runtime.evaluation.credit_recovery import authorize_credit_retry
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from tests.test_source_tool_recovery import invoke, prepare


def export_saved(journal, directory, monkeypatch):
    """The exporter and charged_rows are real; adapt only persistence/archive paging."""
    manifest_id = uuid4()
    materials = journal.blob(b'{"exact":"saved material bytes"}').model_dump(mode="json")
    journal.snapshot.update(
        command=dict(
            run_id=str(journal.root["run_id"]),
            case_id="source-usage",
            limits=dict(max_total_input_tokens=1000000, max_elapsed_seconds=3600),
        ),
        materials_blob=materials,
        stages=[journal.root["stage"]],
    )
    journal.run = lambda run: dict(status="needs_attention", failure_code=None, manifest_id=manifest_id)
    journal.authorize_many = lambda refs: None
    monkeypatch.setattr(exporting, "EvaluationJournal", lambda *args: journal)
    monkeypatch.setattr(
        "podgen.runtime.evaluation.elapsed_budget.ElapsedBudget.load",
        lambda *args: SimpleNamespace(authorizations=()),
    )
    monkeypatch.setattr(
        "podgen.runtime.evaluation.total_input_recovery.effective_ceiling", lambda *args: 1000000
    )
    # These cases have no editorial file graph; source accounting retains its actual manifest.
    monkeypatch.setattr(
        "podgen.runtime.evaluation.editorial_input_export.export_inputs", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("podgen.runtime.evaluation.finalization.read_finalization", lambda *args: None)
    monkeypatch.setattr(export_archive, "event_pages", lambda j, root: iter([j.events(root)]))
    monkeypatch.setattr(export_archive, "turn_pages", lambda j, root: iter([model_turns.rows(j, root)]))
    result = export_run(journal.repo, journal.project, journal.root["run_id"], directory)
    assert json.loads((directory / "run.json").read_text()) == result
    assert (directory / "inputs.json").read_bytes() == b'{"exact":"saved material bytes"}'
    return result


@pytest.mark.parametrize(
    "mode,expected_input,expected_output,unknown",
    [
        ("complete", 550, 30, False),
        ("budget", 100, 10, False),
        ("credit", 100, 10, False),
        ("unknown", 450, 10, True),
    ],
)
@pytest.mark.parametrize("root_aggregate", [True, False])
def test_actual_export_uses_submitted_physical_turns_even_without_stage_aggregate(
    monkeypatch, tmp_path, mode, expected_input, expected_output, unknown, root_aggregate
):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, mode)
    if mode == "complete":
        server.failed = True  # Serve the complete dialogue without the intentional credit refusal.
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        if mode == "complete":
            assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
            assert journal.root["input_tokens"] == 550
        else:
            with pytest.raises(RunFailure):
                invoke(gateway, journal.root, profile, limits)
    if not root_aggregate:
        # Immutable interrupted stage headers may not yet have a semantic aggregate.
        journal.root["input_tokens"] = None
    result = export_saved(journal, tmp_path / "export", monkeypatch)
    assert result["input_tokens"] == expected_input
    assert result["output_tokens_known"] == expected_output
    assert bool(result["unknown_usage_invocations"]) is unknown
    if unknown:
        turn = model_turns.rows(journal, journal.root)[-1]
        assert result["unknown_usage_invocations"] == [str(turn["invocation_id"])]
    if not root_aggregate:
        assert journal.root["input_tokens"] is None


def test_actual_export_credit_retry_counts_parent_paid_turn_once(monkeypatch, tmp_path):
    journal, profile, limits, corpus, server, handler = prepare(monkeypatch, "credit")
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        with pytest.raises(RunFailure):
            invoke(gateway, journal.root, profile, limits)
        journal.root["input_tokens"] = None
        original = journal.root.copy()
        before = export_saved(journal, tmp_path / "before", monkeypatch)
        assert (before["input_tokens"], before["output_tokens_known"]) == (100, 10)
        retry = authorize_credit_retry(
            journal, original["run_id"], original["invocation_id"], guard=lambda: None
        )
        assert invoke(gateway, retry, profile, limits, dict(input=100, output=10)) == '{"answer":"done"}'
    result = export_saved(journal, tmp_path / "after", monkeypatch)
    assert (result["input_tokens"], result["output_tokens_known"]) == (550, 30)
    assert result["unknown_usage_invocations"] == []
    assert len(result["invocations"]["draft"]["attempts"]) == 2
    assert [int(x["metadata"]["podgen_source_turn"]) for x in server.sent] == [0, 1, 1, 2]
    assert len(corpus.calls) == 2
