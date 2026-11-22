"""Full native first audit, revision, both role memories, retained H evidence, preview and replay."""

import json

from podgen import writing_cli
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot
from podgen.runtime.evaluation.profiles import create_profile, load_profile
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.workflow import profile_for
from tests.reading_audit_provider import AuditProvider
from tests.test_editorial_independence import invoke
from tests.test_independent_readings import setup_review
from tests.test_own_plan import own_dataset as own_dataset


def test_full_audit_memory_retains_old_read_without_reexecuting_it_and_replays(
    own_dataset, tmp_path, monkeypatch
):
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
    provider = AuditProvider()
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *a, **k: provider)
    monkeypatch.setattr(
        "podgen.runtime.models.anthropic_provider.AnthropicProvider", lambda *a, **k: provider
    )
    assert invoke(prefix, "run", **options) == 0
    journal = EvaluationJournal(repo, project)
    run_id = options["run_id"]
    manifest = journal.manifest(run_id)
    command = WritingRunCommand.model_validate(manifest["command"])
    assert manifest["reading_audit_contract"] == "reading-audit/1"
    assert manifest["reading_audit_consistency_contract"] == "decision-findings/4"
    assert manifest["reading_audit_memory_projection"] == "shared-packet-evidence/2"
    fact_calls = [
        body for body in provider.requests if body["metadata"]["podgen_stage"].startswith("fact-review")
    ]
    assert len(fact_calls) == 4  # One tool call and three final reviews; no extra read for H.
    for number in (1, 2):
        reader = request_payload(journal.request(journal.invocation(run_id, f"reader-review-{number}")))[
            "audit_context"
        ]
        fact = request_payload(journal.request(journal.invocation(run_id, f"fact-review-{number}")))[
            "audit_context"
        ]
        assert "previous_evidence" not in reader and "packet_evidence_index" not in reader
        assert reader["previous_recommendation"] == "keep"
        assert all(task["origin"] == "fact" for task in fact["assignments"])
        assert fact["previous_evidence"][0]["original_id"] == "E1"
        assert len(fact["previous_evidence"]) == 1
        assert dict(from_review="current", evidence_id="B1") in fact["previous_decisions"][0]["source_refs"]
        writer = request_payload(journal.request(journal.invocation(run_id, f"revise-{number}")))
        assert writer["review_source_passages"][0]["text"] == fact["previous_evidence"][0]["content"]["text"]
        assert "audit_context" not in writer and "FULL_PERSISTED_REASON" not in json.dumps(writer)
        stored = journal.output(journal.invocation(run_id, f"fact-review-{number}"))
        assert len(stored["output"]["audit"]["claim_checks"][0]["rationale"]) > 1600
        assert stored["reading_audit"]["resolved_evidence"]["previous:H1"]["original_id"] == "E1"
        assert stored["reading_audit"]["binding"]["reused_packet_evidence"] == {"current:B1": "B1"}
        assert stored["reading_audit"]["resolved_evidence"]["current:B1"]["content"]["text"]
    generation = generation_repo(repo)
    materials = load_editorial_materials(generation, project, command.author_ref, case_id=command.case_id)
    for stage in (
        "reader-review",
        "fact-review",
        "reader-review-1",
        "fact-review-1",
        "reader-review-2",
        "fact-review-2",
    ):
        row = journal.invocation(run_id, stage)
        record = journal.output(row)
        script = journal.read(RuntimeRef.model_validate(record["script_ref"]))["output"]
        snapshot = load_preview_snapshot(journal, generation, run_id, stage, materials)
        profile = load_profile(generation, project, profile_for(command, stage), stage)
        assert preview_request(
            run_id, stage, profile, materials, snapshot=snapshot, draft=script
        ) == journal.request(row)
    count = len(provider.requests)
    assert (
        writing_cli.main(
            [*prefix, "resume", "--run-id", str(run_id), "--export-dir", str(tmp_path / "export")]
        )
        == 0
    )
    assert len(provider.requests) == count
    human = (tmp_path / "export/review-result.md").read_text()
    assert "Effect of the revision" in human and "F1" in human and "still_open" in human
