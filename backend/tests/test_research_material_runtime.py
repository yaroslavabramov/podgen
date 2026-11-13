"""Hosted report → reviewed/revised plan → writer → source-backed audit → revision/replay."""

import json
from uuid import uuid4

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot
from podgen.runtime.evaluation.profiles import create_profile, load_profile
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.evaluation.workflow import profile_for
from tests.research_material_cases import REPORT, saved
from tests.research_material_provider import ResearchProvider


def test_full_research_workflow_preserves_report_and_only_original_source_evidence(
    repo, scope, producer, monkeypatch, tmp_path
):
    project, _ = scope
    ref, *_ = saved(repo, scope, producer, monkeypatch)
    profiles = {}
    for role, model, provider, roles in (
        ("writer", "claude-opus-5-5", "anthropic", ["writer"]),
        ("reviewer", "gpt-6.1-terra", "openai", ["editorial_reviewer", "plan_reviewer"]),
        ("planner", "gpt-6.1-sol", "openai", ["planner"]),
    ):
        profiles[role] = create_profile(
            repo,
            project,
            model=model,
            provider=provider,
            roles=roles,
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="hosted-case",
        input_contract="editorial-role-packets/1",
        author_ref=ref,
        writer_profile_ref=profiles["writer"],
        reviewer_profile_ref=profiles["reviewer"],
        planner_profile_ref=profiles["planner"],
        plan_reviewer_profile_ref=profiles["reviewer"],
        behavior_profile="production/1",
        plan_mode="own-plan",
        review_mode="independent-readings/1",
        max_plan_revisions=1,
        max_script_revisions=2,
    )
    provider = ResearchProvider()
    evaluation = WritingEvaluation(repo, lambda profile: provider)
    evaluation.run(command)
    journal = EvaluationJournal(repo, project)
    manifest = journal.manifest(command.run_id)
    assert manifest["research_material_contract"]["instructions"] == "research-material-reading/1"
    assert manifest["research_material_contract"]["schema"] == "research-evidence-descriptions/1"
    for stage in ("fact-review", "fact-review-1", "fact-review-2"):
        fields = manifest["response_schemas"][stage]["$defs"]["SourcedFactFinding"]["properties"]
        assert "report sections relevant to this finding" in fields["support_labels"]["description"]
        assert "R report-reading receipts are not source evidence" in fields["source_read_ids"]["description"]
    assert manifest["source_tool_contract"]["version"] == "frozen-corpus-tools/7"
    assert manifest["source_tool_contract"]["default_read_chars"] == 9600
    assert manifest["prompt_cache_layout"] == "research-role-prefix/2"
    assert manifest["provider_contracts"]["draft"]["api_version"] == "2026-10-21"
    stages = [b["metadata"]["podgen_stage"] for b in provider.requests]
    assert set(stages) == {
        "plan",
        "plan-review",
        "plan-revise",
        "plan-review-revised",
        "draft",
        "reader-review",
        "fact-review",
        "revise-1",
        "reader-review-1",
        "fact-review-1",
    }
    draft = request_payload(journal.request(journal.invocation(command.run_id, "draft")))
    assert draft["support"][0]["text"] == REPORT[: REPORT.index("## Consequences")]
    assert "source_excerpts" not in draft["support"][0]
    fact = request_payload(journal.request(journal.invocation(command.run_id, "fact-review")))
    assert fact["evidence"] == [] and all("text" not in row for row in fact["research_contents"])
    revision = request_payload(journal.request(journal.invocation(command.run_id, "revise-1")))
    assert revision["review_source_passages"][0]["source_id"] == "S11"
    assert revision["revision_tasks"][0]["support_refs"] == [dict(card_id="M1")]
    assert revision["revision_tasks"][0]["source_read_refs"] == ["P1"]
    recheck = request_payload(journal.request(journal.invocation(command.run_id, "fact-review-1")))
    assert recheck["audit_context"]["previous_evidence"][0]["original_id"] == "E2"
    assert "R1" not in json.dumps(recheck["audit_context"]["previous_evidence"])
    generation = generation_repo(repo)
    materials = load_editorial_materials(generation, project, ref, case_id="hosted-case")
    for stage in (
        "plan",
        "plan-review",
        "plan-revise",
        "plan-review-revised",
        "draft",
        "fact-review",
        "fact-review-1",
    ):
        row = journal.invocation(command.run_id, stage)
        snapshot = load_preview_snapshot(journal, generation, command.run_id, stage, materials)
        assert snapshot.instructions == journal.request(row)["instructions"]
        monkeypatch.setattr(
            "podgen.writing.research_materials.runtime_instructions.BODY",
            "Changed current adapter must not alter frozen instructions.",
        )
        record = journal.output(row)
        inputs = {}
        if stage in ("plan-review", "plan-review-revised", "plan-revise"):
            inputs["candidate"] = journal.read(RuntimeRef.model_validate(record["candidate_plan_ref"]))[
                "output"
            ]
        if stage == "plan-revise":
            inputs["plan_review"] = journal.read(RuntimeRef.model_validate(record["plan_review_ref"]))[
                "output"
            ]
        if stage == "draft":
            inputs["approved_plan"] = journal.read(RuntimeRef.model_validate(record["accepted_plan_ref"]))[
                "output"
            ]
        if stage.startswith("fact-review"):
            inputs["draft"] = journal.read(RuntimeRef.model_validate(record["script_ref"]))["output"]
        profile = load_profile(generation, project, profile_for(command, stage), stage)
        assert preview_request(
            command.run_id, stage, profile, materials, snapshot=snapshot, **inputs
        ) == journal.request(row)
    count = len(provider.requests)
    evaluation.run(command)
    assert len(provider.requests) == count

    from podgen.runtime.evaluation.export import export_run

    destination = tmp_path / "export"
    export_run(repo, project, command.run_id, destination)
    assert json.loads((destination / "archive/revise-1/request.json").read_text()) == journal.request(
        journal.invocation(command.run_id, "revise-1")
    )
    assert (destination / "review-result.md").read_text()
    assert (destination / "research/report.md").read_text() == REPORT
    assert (destination / "research/sources/S11.txt").read_text()
    assert len(json.loads((destination / "research/sources.json").read_text())) == 2
