"""A real journal and local OpenAI wire retain research and both sides of credit recovery."""

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest

from podgen.domain.errors import ContractError
from podgen.research.hosted.journal import ResearchJournal
from podgen.runtime.evaluation import model_turns
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects, Measurements
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import ProviderFault, WireReply
from podgen.runtime.models.openai_provider import OpenAIProvider
from tests.research_material_provider import ResearchProvider
from tests.source_acquisition_support import ARTICLE
from tests.test_research_material_resolver import fixture, save_report


@pytest.mark.parametrize("target_stage", ["fact-review", "revise-1"])
def test_research_credit_resume_persists_full_audit_and_replays_without_provider_work(
    repo, scope, producer, monkeypatch, target_stage
):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    repo.objects = MeasuredObjects(repo.objects.root)
    measured = Measurements(repo.database, repo.objects)
    research_turn = 1 if target_stage == "fact-review" else 0
    credit_turn = research_turn + 1
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
    provider, wire, refused = ResearchProvider(), [], False

    def handler(request):
        nonlocal refused
        body = json.loads(request.content)
        wire.append((request.url.path, deepcopy(body)))
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        stage, turn = body["metadata"]["podgen_stage"], int(body["metadata"].get("podgen_source_turn", 0))
        # The local oracle reads only the initial commission. The SDK still sends
        # the complete native conversation and the strict production parser stays intact.
        first_user = next(item for item in body["input"] if item.get("role") == "user")
        semantic = dict(body, input=[first_user])
        if stage == target_stage and turn == credit_turn and not refused:
            refused = True
            return httpx2.Response(429, json=dict(error=dict(code="credit_balance_exhausted")))
        response = json.loads(provider.submit(semantic, client_request_id="local").body)
        if stage == target_stage and turn == research_turn:
            script = request_payload(semantic)["script"]["utterances"]
            response["output"] = [
                dict(
                    type="message",
                    role="assistant",
                    content=[
                        dict(
                            type="output_text",
                            text=json.dumps(
                                dict(
                                    research_requests=[
                                        dict(
                                            question="What are the exact original words?",
                                            why_needed="Verify this scoped claim.",
                                            anchors=[dict(utterance_index=0, quote=script[0]["text"])],
                                        )
                                    ]
                                )
                            ),
                        )
                    ],
                )
            ]
        elif stage == "fact-review" and turn == 2:
            response["output"] = [
                dict(
                    type="function_call",
                    call_id="read_after_credit",
                    name="read_source",
                    status="completed",
                    arguments=json.dumps(dict(source_id="S11", start=150, end=250)),
                )
            ]
        elif stage == "fact-review" and turn == 3:
            value = json.loads(response["output"][0]["content"][0]["text"])
            tool_results = [
                json.loads(part["text"])
                for item in body["input"]
                if item.get("type") == "function_call_output"
                for part in item["output"]
                if part.get("type") == "input_text"
            ]
            read_ids = [result["passage"]["read_id"] for result in tool_results if "passage" in result]
            value["audit"]["claim_checks"][0]["source_refs"] = [
                dict(from_review="current", evidence_id=read_id) for read_id in read_ids
            ]
            response["output"][0]["content"][0]["text"] = json.dumps(value)
        return httpx2.Response(200, json=response)

    journal = EvaluationJournal(repo, scope[0])

    class LocalWriter:
        def count(self, body):
            return provider.count(body)

        def submit(self, body, *, client_request_id):
            reply = handler(httpx2.Request("POST", "https://local.invalid/responses", json=body))
            if reply.status_code != 200:
                raise ProviderFault("provider_http_429", reply=WireReply(reply.content, 429))
            value = reply.json()
            value["anthropic_content"] = [
                dict(type="text", text=part["text"])
                for item in value["output"]
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ]
            return WireReply(json.dumps(value).encode())

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        local = OpenAIProvider("offline-test-secret-unique", http_client=client)
        evaluation = WritingEvaluation(
            repo,
            lambda profile: (
                local
                if profile.provider == "openai"
                else LocalWriter()
                if target_stage == "revise-1"
                else provider
            ),
            material_acquirer=acquire,
        )

        def run(phase, **kwargs):
            with measured.phase(phase):
                return evaluation.run(command, **kwargs)

        with pytest.raises(RunFailure, match="source_material_handoff_pending"):
            run("research_handoff")
        root = journal.invocation(command.run_id, target_stage)
        pending = next(
            row["data"]["result"]
            for row in model_turns.events(journal, root, kinds={"source_material_pending"})
            if row["data"]["name"] == "request_research"
        )
        assert pending["status"] == "pending"
        child = ResearchJournal(repo, scope[0], UUID(pending["child_run_id"]))
        save_report(
            child,
            json.dumps(
                dict(
                    answer="The original words are retained.",
                    limitations="",
                    evidence=[
                        dict(url="https://example.com/article", quote=ARTICLE[:80], relation="Exact account.")
                    ],
                )
            ),
        )
        with pytest.raises(RunFailure):
            run("credit_refusal")
        root = journal.invocation(command.run_id, target_stage)
        assert refused and root["status"] == "failed"
        run("credit_recovery_and_recheck", retry_credit_refusal=root["invocation_id"])
        retry = journal.invocation(command.run_id, target_stage)
        assert retry["status"] == "completed"
        assert retry["attempt_kind"] == "credit_retry"
        with pytest.raises(ContractError, match="invalid_multipart_user_payload"):
            request_payload(journal.request(retry))
        if target_stage == "fact-review":
            resolved = journal.output(retry)["reading_audit"]["resolved_evidence"]
            assert {value["origin_invocation_id"] for value in resolved.values()} == {
                str(root["invocation_id"]),
                str(retry["invocation_id"]),
            }
            assert len(resolved) == 2
        assert journal.invocation(command.run_id, "fact-review-1")["status"] == "completed"
        before = len(wire), len(provider.requests), len(fetcher.calls)
        run("completed_replay")
        assert (len(wire), len(provider.requests), len(fetcher.calls)) == before
    submitted = [
        int(body["metadata"]["podgen_source_turn"])
        for path, body in wire
        if path.endswith("/responses") and body["metadata"]["podgen_stage"] == target_stage
    ]
    assert submitted == ([0, 1, 2, 2, 3] if target_stage == "fact-review" else [0, 1, 1])
    destination = os.environ.get("PODGEN_RESEARCH_RECOVERY_MEASUREMENTS")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        metrics = dict(
            measured.snapshot(), target_stage=target_stage, native_requests=len(wire), paid_provider_calls=0
        )
        (directory / (target_stage + ".json")).write_text(json.dumps(metrics, indent=2) + "\n")
