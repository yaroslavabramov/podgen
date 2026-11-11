"""Ordinary product continuation inherits exact production resources and both prior verdicts."""

import json
from copy import deepcopy

import pytest
from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.editorial_continuation.cli import prepare as prepare_continuation
from podgen.runtime.evaluation.editorial_continuation.source import build_command
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.service import WritingEvaluation

from tests.editorial_continuation_cases import EditorialProvider
from tests.source_excerpt_cases import pin_before_reading_audit
from tests.test_editorial_independence import invoke
from tests.test_independent_readings import setup_review
from tests.test_own_plan import own_dataset as own_dataset


def exhausted(own_dataset, tmp_path, monkeypatch, revisions=2):
    pin_before_reading_audit(monkeypatch)
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    repo, project, *_ = own_dataset
    for role, model in (("writer", "claude-opus-5-5"), ("reviewer", "gpt-6.1-terra")):
        ref = create_profile(
            repo,
            project,
            model=model,
            provider="anthropic" if role == "writer" else "openai",
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref
        options[role + "_profile"] = ref.model_dump_json()
    options.update(behavior_profile="production/1", max_script_revisions=revisions)
    provider = EditorialProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *a, **k: provider
    )
    assert invoke(prefix, "run", **options) == 2
    journal = EvaluationJournal(repo, project)
    parent = WritingRunCommand.model_validate(journal.manifest(options["run_id"])["command"])
    return repo, journal, parent, provider, prefix


def test_native_preview_continuation_recheck_and_two_keeps_are_idempotent(own_dataset, tmp_path, monkeypatch):
    repo, journal, parent, provider, prefix = exhausted(own_dataset, tmp_path, monkeypatch)
    frozen_parent = deepcopy(journal.manifest(parent.run_id))
    child = build_command(journal, parent.run_id, additional_input=524288)
    folder = tmp_path / "continued"
    count = len(provider.requests)
    result = prepare_continuation(repo, child, folder)
    assert result["endpoint_called"] is False and len(provider.requests) == count
    preview = json.loads((folder / "revise-1-preview.json").read_text())["request"]
    assert request_payload(preview)["revision_operation"]["script_version"] == 2
    assert prepare_continuation(repo, child, folder)["endpoint_called"] is False
    with pytest.raises(ContractError, match="allowance_changed"):
        build_command(journal, parent.run_id, additional_input=524289)
    provider.continuing = True
    args = [
        *prefix,
        "continue-editorial",
        "--source-run-id",
        str(parent.run_id),
        "--additional-input-tokens",
        "524288",
        "--export-dir",
        str(folder),
    ]
    assert writing_cli.main(args) == 0
    new = provider.requests[count:]
    assert [body["metadata"]["podgen_stage"] for body in new] == [
        "revise-1",
        "reader-review-1",
        "fact-review-1",
    ]
    assert journal.request(journal.invocation(child.run_id, "revise-1")) == preview
    assert {k: v for k, v in new[0].items() if k != "metadata"} == {
        k: v for k, v in preview.items() if k != "metadata"
    }
    assert new[0]["metadata"]["podgen_source_turn"] == "0"
    comparison = request_payload(new[1])["revision_check"]
    assert comparison["previous_reader_decision"]["recommendation"] == "keep"
    assert comparison["revision_operation"]["script_version"] == 2
    assert comparison["assignments"] and all(row["id"].startswith("F") for row in comparison["assignments"])
    sent = len(provider.requests)
    assert (
        writing_cli.main([*prefix, "resume", "--run-id", str(child.run_id), "--export-dir", str(folder)]) == 0
    )
    assert len(provider.requests) == sent
    assert journal.manifest(parent.run_id) == frozen_parent
    result = export_run(repo, parent.project_id, child.run_id, folder)
    assert result["review_status"] == "keep" and result["revisions_completed"] == 3
    assert result["usage_ledger"]["own"] == dict(input=300, output=300)
    assert result["usage_ledger"]["total"]["input"] == result["usage_ledger"]["inherited"]["input"] + 300


def test_chained_continuations_stop_at_global_third_revision_and_keep_costs(
    own_dataset, tmp_path, monkeypatch
):
    repo, journal, parent, provider, _ = exhausted(own_dataset, tmp_path, monkeypatch, revisions=1)
    provider.continuing = provider.reject_fact = True
    runner = WritingEvaluation(repo, lambda _: provider)
    child = build_command(journal, parent.run_id, additional_input=1000)
    one = runner.run(child)
    assert one["review_status"] == "revision_limit_exhausted" and one["revisions_completed"] == 2
    grandchild = build_command(journal, child.run_id, additional_input=0)
    two = runner.run(grandchild)
    assert two["review_status"] == "revision_limit_exhausted" and two["revisions_completed"] == 3
    assert two["usage_ledger"]["inherited"] == one["token_usage_or_reservation"]
    assert two["token_usage_or_reservation"]["input"] == one["token_usage_or_reservation"]["input"] + 300
    with pytest.raises(ContractError, match="revision_limit_exhausted"):
        build_command(journal, grandchild.run_id, additional_input=1000)


def test_parent_planning_continuation_keeps_clearance_and_earlier_costs(own_dataset, tmp_path, monkeypatch):
    from podgen.runtime.evaluation.planning_continuation.source import build_command as continue_plan

    from tests.planning_continuation_cases import ContinuationProvider, failed_source

    original, journal, _ = failed_source(own_dataset, tmp_path, monkeypatch)
    planner, editor = ContinuationProvider("accept"), EditorialProvider()
    editor.legacy = True

    class Provider:
        count = editor.count

        def submit(self, body, *, client_request_id):
            provider = planner if body["metadata"]["podgen_stage"].startswith("plan") else editor
            return provider.submit(body, client_request_id=client_request_id)

    parent = continue_plan(journal, original.run_id, limit=1)
    repo = own_dataset[0]
    stopped = WritingEvaluation(repo, lambda _: Provider()).run(parent)
    assert stopped["review_status"] == "revision_limit_exhausted"
    child = build_command(journal, parent.run_id, additional_input=1000)
    editor.continuing = True
    final = WritingEvaluation(repo, lambda _: editor).run(child)
    frozen = journal.manifest(child.run_id)
    assert "planning_continuation" not in frozen
    assert child.planning_parent_run_id is None and child.planning_continuation_limit is None
    binding = frozen["editorial_continuation"]
    proof = journal.read(RuntimeRef.model_validate(binding["accepted_plan"]["clearance_ref"]))
    assert proof["run_id"] == str(parent.run_id)
    earlier = binding["planning_ancestry_usage"]
    assert len(earlier) == 1 and earlier[0]["run_id"] == str(original.run_id)
    assert earlier[0]["token_usage_or_reservation"]["input"] > 0
    assert (
        final["token_usage_or_reservation"]["input"]
        == stopped["token_usage_or_reservation"]["input"]
        + earlier[0]["token_usage_or_reservation"]["input"]
        + 300
    )
    assert final["review_status"] == "keep"
