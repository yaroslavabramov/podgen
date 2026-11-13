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
        else:
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
    WritingEvaluation(repo, lambda _: ResearchProvider()).run(command)
    generation, journal, run_id = generation_repo(repo), EvaluationJournal(repo, project), uuid4()
    prepared = prepare(repo, generation, journal, command.run_id, run_id)
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
    assert len(provider.requests) == 2
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
    assert len(provider.requests) == 2
    with pytest.raises(ContractError, match="standalone_fact_experiment_requires_its_runner"):
        WritingEvaluation(repo, lambda _: pytest.fail("full workflow invoked")).run(prepared.command)
