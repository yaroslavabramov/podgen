"""A retained decision that omits frozen evidence: /3 carries it; a saved /2 rejection is readmitted locally."""

import json

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.usage import attempt_usage, unsent_without_generation
from tests.reading_audit_provider import AuditProvider
from tests.test_editorial_independence import invoke
from tests.test_independent_readings import setup_review
from tests.test_own_plan import own_dataset as own_dataset

PREVIOUS = dict(from_review="previous", evidence_id="H1")
PACKET = dict(from_review="current", evidence_id="B1")


class RetainedWithoutFrozenEvidence(AuditProvider):
    """The first factual recheck keeps the old verdict but cites only part of its frozen evidence."""

    def submit(self, body, *, client_request_id):
        reply = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] != "fact-review-1":
            return reply
        response = json.loads(reply.body)
        content = response["output"][0]["content"][0]
        value = json.loads(content["text"])
        value["audit"]["claim_checks"][0]["source_refs"] = [PACKET]
        value["previous_dispositions"][0]["disposition"] = "retained"
        content["text"] = json.dumps(value, ensure_ascii=False)
        return type(reply)(json.dumps(response, ensure_ascii=False).encode())


def start(own_dataset, tmp_path, monkeypatch):
    repo, project, *_ = own_dataset
    prefix, options, _ = setup_review(own_dataset, tmp_path, monkeypatch)
    for role, model in (("writer", "claude-opus-5-5"), ("reviewer", "gpt-6.1-terra")):
        ref = create_profile(
            repo,
            project,
            model=model,
            provider="anthropic" if role == "writer" else "openai",
            output_limit=24576,
            context_limit=200000,
            reasoning_effort="medium",
        ).ref
        options[role + "_profile"] = ref.model_dump_json()
    options["behavior_profile"] = "production/1"
    provider = RetainedWithoutFrozenEvidence()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *a, **k: provider
    )
    return prefix, options, provider, EvaluationJournal(repo, project)


def stages(provider):
    return [body["metadata"]["podgen_stage"] for body in provider.requests]


def resume(prefix, run_id, tmp_path, *extra):
    return writing_cli.main(
        [*prefix, "resume", "--run-id", str(run_id), "--export-dir", str(tmp_path / "export"), *extra]
    )


def test_archived_v3_carries_retained_evidence_and_replays(own_dataset, tmp_path, monkeypatch):
    monkeypatch.setattr("podgen.runtime.evaluation.manifest.CONSISTENCY_VERSION", "decision-findings/3")
    prefix, options, provider, journal = start(own_dataset, tmp_path, monkeypatch)
    assert invoke(prefix, "run", **options) == 0
    run_id = options["run_id"]
    assert journal.manifest(run_id)["reading_audit_consistency_contract"] == "decision-findings/3"
    row = journal.invocation(run_id, "fact-review-1")
    assert row["attempt_kind"] == "role"
    stored = journal.output(row)
    assert stored["wire_output"]["audit"]["claim_checks"][0]["source_refs"] == [PACKET]
    assert stored["output"]["audit"]["claim_checks"][0]["source_refs"] == [PACKET, PREVIOUS]
    assert stored["reading_audit"]["retained_evidence_carried"] == [
        dict(previous_decision_id="D1", current_decision_ids=["D1"], source_refs=[PREVIOUS])
    ]
    assert "admission_contract" not in stored["reading_audit"]
    assert stored["reading_audit"]["resolved_evidence"]["previous:H1"]["original_id"] == "E1"
    count = len(provider.requests)
    assert resume(prefix, run_id, tmp_path) == 0
    assert len(provider.requests) == count


def test_saved_rejection_is_readmitted_once_without_a_model_call(own_dataset, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("podgen.runtime.evaluation.manifest.CONSISTENCY_VERSION", "decision-findings/2")
    prefix, options, provider, journal = start(own_dataset, tmp_path, monkeypatch)
    assert invoke(prefix, "run", **options) == 2
    run_id = options["run_id"]
    assert journal.manifest(run_id)["reading_audit_consistency_contract"] == "decision-findings/2"
    rejected = journal.invocation(run_id, "fact-review-1")
    assert rejected["status"] == "validation_failed"
    assert journal.run(run_id)["failure_code"] == "independent_reading_output_invalid"
    paid = stages(provider)
    assert paid[-1] == "fact-review-1"

    # Only the rejected current reading is eligible; a completed one is not.
    completed = journal.invocation(run_id, "fact-review")
    capsys.readouterr()
    assert resume(prefix, run_id, tmp_path, "--readmit-reading", str(completed["invocation_id"])) == 2
    assert json.loads(capsys.readouterr().err)["code"] == "local_readmission_not_eligible"
    assert len(journal.attempts(run_id, "fact-review-1")) == 1

    assert resume(prefix, run_id, tmp_path, "--readmit-reading", str(rejected["invocation_id"])) == 0
    after = stages(provider)[len(paid) :]
    assert "fact-review-1" not in after and after == ["revise-2", "reader-review-2", "fact-review-2"]
    row = journal.invocation(run_id, "fact-review-1")
    assert (row["attempt_kind"], row["operation_kind"], row["status"]) == (
        "local_readmission",
        "role",
        "completed",
    )
    assert row["parent_invocation_id"] == rejected["invocation_id"]
    assert attempt_usage(journal, row, 24576) == dict(input=0, output=0)
    stored = journal.output(row)
    assert stored["wire_output"]["audit"]["claim_checks"][0]["source_refs"] == [PACKET]
    assert stored["output"]["audit"]["claim_checks"][0]["source_refs"] == [PACKET, PREVIOUS]
    assert stored["reading_audit"]["admission_contract"] == "decision-findings/3"
    assert stored["reading_audit"]["retained_evidence_carried"][0]["source_refs"] == [PREVIOUS]
    assert journal.manifest(run_id)["reading_audit_consistency_contract"] == "decision-findings/2"
    authorization = journal.read(
        RuntimeRef(record_kind="tool_execution", record_id=row["request_record_id"])
    )["authorization"]
    assert authorization["kind"] == "local-readmission/2"
    assert authorization["failure_code"] == "audit_retained_evidence_missing"
    assert authorization["target_invocation_id"] == str(rejected["invocation_id"])

    # Replay re-verifies the readmitted report and repeats no work; the grant is single-use.
    count = len(provider.requests)
    assert resume(prefix, run_id, tmp_path) == 0
    assert resume(prefix, run_id, tmp_path, "--readmit-reading", str(rejected["invocation_id"])) == 0
    assert len(provider.requests) == count
    assert len(journal.attempts(run_id, "fact-review-1")) == 2
    exported = json.loads((tmp_path / "export/run.json").read_text())
    assert exported["status"] != "needs_attention"
    assert exported["unknown_usage_invocations"] == []


def test_a_readmitted_selection_repair_has_no_generation_or_unknown_usage():
    row = dict(
        attempt_kind="local_readmission",
        operation_kind="anchor_repair",
        input_tokens=None,
        output_tokens=None,
    )
    assert unsent_without_generation(None, row)
    assert attempt_usage(None, row, 24576) == dict(input=0, output=0)
