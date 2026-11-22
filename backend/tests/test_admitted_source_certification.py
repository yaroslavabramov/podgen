"""A source admitted during a factual reading stays readable when the finished cycle is certified."""

import json
from uuid import uuid4

from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.reading_outcome import read_outcome
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply
from tests.research_material_provider import ResearchProvider
from tests.test_research_material_resolver import fixture


class AdmittingProvider(ResearchProvider):
    """The first factual reading requests a new document, reads it and cites that read."""

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        turn = int(body["metadata"].get("podgen_source_turn", 0))
        calls = {
            0: (
                "request_source",
                dict(source_id=None, url="https://example.com/new", reason="Check the date."),
            ),
            1: ("read_source", dict(source_id="S12", start=0, end=150)),
        }
        if stage != "fact-review" or turn not in calls:
            return super().submit(body, client_request_id=client_request_id)
        self.requests.append(body)
        name, arguments = calls[turn]
        return WireReply(
            json.dumps(
                dict(
                    id=f"resp_admit_{turn}",
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(
                            type="function_call",
                            call_id=f"admit_{turn}",
                            name=name,
                            arguments=json.dumps(arguments),
                            status="completed",
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                )
            ).encode()
        )


def test_certification_replays_a_reading_of_a_runtime_admitted_source(repo, scope, producer, monkeypatch):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    profiles = {}
    for role, model, provider, roles in (
        ("writer", "claude-opus-5-5", "anthropic", ["writer"]),
        ("reviewer", "gpt-6.1-terra", "openai", ["editorial_reviewer", "plan_reviewer"]),
        ("planner", "gpt-6.1-sol", "openai", ["planner"]),
    ):
        profiles[role] = create_profile(
            repo,
            scope[0],
            model=model,
            provider=provider,
            roles=roles,
            context_limit=200000,
            output_limit=24576,
            reasoning_effort="medium",
        ).ref
    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=scope[0],
        actor_id=repo.actor_id,
        case_id=materials.packet.assignment.key,
        input_contract="editorial-role-packets/1",
        author_ref=materials.shared_ref,
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
    provider = AdmittingProvider()
    evaluation = WritingEvaluation(repo, lambda profile: provider, material_acquirer=acquire)
    result = evaluation.run(command)
    journal = EvaluationJournal(repo, scope[0])
    first = journal.output(journal.invocation(command.run_id, "fact-review"))
    cited = first["reading_audit"]["resolved_evidence"]["current:E2"]
    assert cited["content"]["source_id"] == "S12"
    assert cited["source_selectors"][0]["key"] == "admission:S12"
    assert result["status"] != "needs_attention"
    assert read_outcome(journal, command.run_id) is not None
    count, fetched = len(provider.requests), len(fetcher.calls)
    evaluation.run(command)
    assert (len(provider.requests), len(fetcher.calls)) == (count, fetched)
