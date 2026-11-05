"""Bounded selector recovery preserves failed history and exact editorial judgments."""

import json
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import query
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.test_writing_evaluation import FakeProvider, run
from tests.test_writing_evaluation import setup_writer as setup_writer


@pytest.mark.parametrize("automatic", [False, True])
def test_single_anchor_recovery_keeps_original_and_counts_all_usage(repo, setup_writer, tmp_path, automatic):
    command, loader, _ = setup_writer
    if automatic:
        command = command.model_copy(update={"review_anchor_policy": "bounded-selection-v1"})
    provider = FakeProvider(fault="anchor")
    journal = EvaluationJournal(repo, command.project_id)
    if not automatic:
        with pytest.raises(RunFailure, match="role_output_validation_failed"):
            run(repo, command, loader, provider)
        assert len(provider.requests) == 2
    result = run(repo, command, loader, provider, repair=not automatic)
    assert result["status"] == "completed_unaccepted"
    assert result["token_usage_or_reservation"] == dict(input=400, output=492)
    assert len(provider.requests) == 4
    attempts = journal.attempts(command.run_id, "review")
    assert [a["status"] for a in attempts] == ["validation_failed", "completed"]
    original, repaired = attempts
    assert repaired["parent_invocation_id"] == original["invocation_id"]
    assert original["invocation_id"] != repaired["invocation_id"]
    assert journal.request(original) == provider.requests[1]
    assert journal.request(repaired)["input"][:-1] == provider.requests[1]["input"]
    assert journal.request(repaired)["text"]["format"]["name"] == "anchor_choices"
    response = json.loads(journal.last_response(original))
    rejected = json.loads(response["output"][0]["content"][0]["text"])
    review = journal.output(repaired)["output"]
    before = rejected["strengths"][0].pop("anchors")
    after = review["strengths"][0].pop("anchors")
    assert rejected == review
    assert before != after
    draft = journal.output(journal.invocation(command.run_id, "draft"))["output"]
    assert after[0]["quote"] in draft["utterances"][after[0]["utterance_index"]]["text"]
    assert run(repo, command, loader, provider, repair=True) == result
    assert len(provider.requests) == 4
    exported = export_run(repo, command.project_id, command.run_id, tmp_path / "all-attempts")
    assert exported["input_tokens"] == 400
    assert exported["output_tokens_known"] == 492
    assert len(exported["invocations"]["review"]["attempts"]) == 2
    assert (tmp_path / "all-attempts/archive/review/request.json").is_file()
    assert (tmp_path / "all-attempts/archive/review/attempt-1/request.json").is_file()
    assert any((tmp_path / "all-attempts/archive/review").glob("*provider_response-response_blob.json"))


@pytest.mark.parametrize(
    "choice,code", [("null", "unresolved_editorial_anchor"), ("unknown", "unknown_anchor_selector")]
)
def test_anchor_recovery_has_one_bounded_attempt(repo, setup_writer, choice, code):
    command, loader, _ = setup_writer
    provider = FakeProvider(fault="anchor")
    provider.repair_choice = choice
    command = command.model_copy(update={"review_anchor_policy": "bounded-selection-v1"})
    with pytest.raises(RunFailure, match=code):
        run(repo, command, loader, provider)
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider, repair=True)
    assert len(provider.requests) == 3
    journal = EvaluationJournal(repo, command.project_id)
    assert len(journal.attempts(command.run_id, "review")) == 2
    assert journal.invocation(command.run_id, "revise") is None


def test_anchor_recovery_charges_failed_attempt_before_submit(repo, setup_writer):
    command, loader, _ = setup_writer
    command = command.model_copy(
        update={
            "review_anchor_policy": "bounded-selection-v1",
            "limits": RunLimits(max_total_input_tokens=250),
        }
    )
    provider = FakeProvider(fault="anchor")
    with pytest.raises(RunFailure, match="run_token_budget_exceeded"):
        run(repo, command, loader, provider)
    assert len(provider.requests) == 2
    assert len(provider.counts) == 3


def test_anchor_repair_submission_crash_never_resends(repo, setup_writer):
    command, loader, _ = setup_writer
    provider = FakeProvider(fault="anchor")
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider)

    def crash():
        raise KeyboardInterrupt()

    provider.on_submit = crash
    with pytest.raises(KeyboardInterrupt):
        run(repo, command, loader, provider, repair=True)
    provider.on_submit = None
    with pytest.raises(RunFailure, match="submission_acknowledgement_unknown"):
        run(repo, command, loader, provider, repair=True)
    assert len(provider.requests) == 3


def test_repair_parent_must_be_original_review_in_same_run(repo, setup_writer):
    from sqlalchemy.exc import IntegrityError

    command, loader, _ = setup_writer
    provider = FakeProvider(fault="anchor")
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider)
    journal = EvaluationJournal(repo, command.project_id)
    draft = journal.invocation(command.run_id, "draft")
    with pytest.raises(IntegrityError):
        with repo.database.transaction() as tx:
            query(
                tx,
                """INSERT INTO evaluation_model_invocations
              (project_id,run_id,invocation_id,stage,request_record_id,status,attempt_index,parent_invocation_id)
              VALUES(:p,:r,:i,'review',:q,'prepared',1,:parent)""",
                p=command.project_id,
                r=command.run_id,
                i=uuid4(),
                q=draft["request_record_id"],
                parent=draft["invocation_id"],
            )


@pytest.mark.parametrize("fault", ["invalid-json", "incomplete", "timeout", "refusal"])
def test_anchor_recovery_rejects_other_review_failures(repo, setup_writer, fault):
    command, loader, _ = setup_writer
    provider = FakeProvider()

    def break_review():
        if len(provider.requests) == 2:
            provider.fault = fault

    provider.on_submit = break_review
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider)
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider, repair=True)
    assert len(provider.requests) == 2
    assert len(EvaluationJournal(repo, command.project_id).attempts(command.run_id, "review")) == 1


def test_repair_checks_source_access_after_token_count(repo, scope, producer, setup_writer):
    from podgen.persistence.dependencies import DependencyLedger
    from tests.factories import Factory

    command, loader, text = setup_writer
    source, _ = Factory(repo, *scope, producer).setup().source()
    bound = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(
        command.project_id,
        bound,
        dict(synthetic_author_text=text, basis_ref=source.ref.model_dump(mode="json")),
    )
    command = command.model_copy(update={"author_ref": bound})
    provider = FakeProvider(fault="anchor")
    with pytest.raises(RunFailure):
        run(repo, command, loader, provider)
    provider.on_count = lambda: DependencyLedger(repo).invalidate(
        command.project_id, source.ref, category="access", reason="SYNTHETIC withdrawal before repair"
    )
    with pytest.raises(AccessDenied):
        run(repo, command, loader, provider, repair=True)
    assert len(provider.requests) == 2
