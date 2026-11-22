"""The isolated runner uses the ordinary durable fact loop, including replay."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import generation_repo
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects, Measurements
from podgen.runtime.evaluation.fact_source_experiment.preparation import admit, prepare
from podgen.runtime.evaluation.fact_source_experiment.service import run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply
from tests.reading_audit_cases import report
from tests.research_material_cases import saved
from tests.research_material_provider import ResearchProvider


class SourceOnlyProvider:
    def __init__(self):
        self.requests = []

    def count(self, body):
        return WireReply(b'{"input_tokens":100}')

    def submit(self, body, *, client_request_id):
        self.requests.append(deepcopy(body))
        assert body["metadata"]["podgen_stage"] == "fact-review"
        assert any(tool.get("type") == "web_search" for tool in body["tools"])
        assert "web_search_call.action.sources" in body.get("include", [])
        data = request_payload(dict(body, input=[x for x in body["input"] if x.get("role") == "user"]))
        assert "research_contents" not in data and "research_results" not in data
        if len(self.requests) == 1:
            assert data["evidence"] == []
            assert data["audit_context"]["previous_decisions"] == []
            output = [
                dict(
                    type="function_call",
                    call_id="read_original",
                    name="read_source",
                    arguments=json.dumps(dict(source_id="S11", start=0, end=150)),
                    status="completed",
                )
            ]
        elif len(self.requests) == 2:
            output = [
                dict(
                    type="function_call",
                    call_id="reopen_read",
                    name="read_work_history",
                    arguments=json.dumps(dict(kind="tool_result", index=0, start=0, end=None)),
                    status="completed",
                )
            ]
        else:
            reopened = next(
                json.loads(item["output"])
                for item in body["input"]
                if item.get("type") == "function_call_output" and item["call_id"] == "reopen_read"
            )
            assert reopened["start"] == 0 and reopened["end"] <= 9600
            assert "E1" in reopened["text"]
            script = dict(
                utterances=[
                    {k: v for k, v in row.items() if k != "index"} for row in data["script"]["utterances"]
                ]
            )
            value = report(script, fact=True, required=True, source_id="E1")
            output = [
                dict(
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text=json.dumps(value))],
                )
            ]
        return WireReply(
            json.dumps(
                dict(
                    id="resp_fake_" + str(len(self.requests)),
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=output,
                    usage=dict(input_tokens=100, output_tokens=100),
                )
            ).encode()
        )


def test_fact_only_runner_pins_once_reads_sources_and_never_repeats_completed_work(
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
    from podgen.writing.prompts import composition
    from podgen.writing.research_materials.source_instruction_variant import KEY as VARIANTS

    read = composition.read
    words = "SYNTHETIC current wording before the source run."
    monkeypatch.setattr(
        composition,
        "read",
        lambda path, **kw: read(path, **kw) + ("\n" + words if path == "fact-review.md" else ""),
    )
    WritingEvaluation(repo, lambda _: ResearchProvider()).run(command)
    source_manifest = EvaluationJournal(repo, project).manifest(command.run_id)
    assert set(source_manifest[VARIANTS]) == {"fact-review"}
    assert words in source_manifest[VARIANTS]["fact-review"]["body"]
    words = "SYNTHETIC changed again after the source run."

    generation, journal, run_id = generation_repo(repo), EvaluationJournal(repo, project), uuid4()
    prepared = prepare(repo, generation, journal, command.run_id, run_id)
    assert "SYNTHETIC current wording before the source run." in prepared.body["instructions"]
    assert words not in prepared.body["instructions"]
    with monkeypatch.context() as frozen:
        frozen.setattr(
            composition,
            "historical_adapter",
            lambda *a, **kw: pytest.fail("Fresh report-off used the frozen fingerprint index"),
        )
        repeated = prepare(repo, generation, journal, command.run_id, run_id)
    assert repeated.body == prepared.body
    initial_hash = digest(prepared.body)
    assert prepared.command.reviewer_profile_ref == command.reviewer_profile_ref
    assert {row["source_id"] for row in prepared.sources} == {
        source.source_id for source in prepared.materials.packet.sources
    }
    with monkeypatch.context() as patch:
        patch.setattr(WritingEvaluation, "_materials", lambda *a: pytest.fail("admission reloaded corpus"))
        admit(repo, journal, prepared)
        admit(repo, journal, prepared)
    assert digest(prepared.body) == initial_hash
    provider = SourceOnlyProvider()
    metrics = Measurements(repo.database, MeasuredObjects(tmp_path / "measurement-only"))
    result = run(
        repo,
        generation,
        journal,
        command.run_id,
        run_id,
        lambda _: provider,
        initial_hash,
        metrics,
        tmp_path / "result",
    )
    assert len(provider.requests) == 3
    assert result.output["findings"][0]["support_labels"] == []
    assert journal.invocation(run_id, "draft") is None
    assert journal.invocation(run_id, "reader-review") is None
    assert journal.invocation(run_id, "plan") is None
    assert journal.run(run_id)["status"] == "completed_unaccepted"
    run(
        repo,
        generation,
        journal,
        command.run_id,
        run_id,
        lambda _: pytest.fail("repeated paid work"),
        initial_hash,
        metrics,
        tmp_path / "replay",
    )
    assert len(provider.requests) == 3
    with pytest.raises(ContractError, match="standalone_fact_experiment_requires_its_runner"):
        WritingEvaluation(repo, lambda _: pytest.fail("full workflow invoked")).run(prepared.command)
