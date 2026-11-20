"""Native tool handoffs and terminal author/reviewer requests retain exact plans and paid work."""

import json
from uuid import UUID

import pytest

from podgen.domain.canonical import digest
from podgen.episodes.production.prepare import prepare
from podgen.episodes.production.writing import saved_command
from podgen.research.hosted.journal import ResearchJournal
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.reusable_plan import export_plan_ref
from podgen.runtime.models.contracts import WireReply
from tests.episode_production_support import REPORT, setup_episode
from tests.hosted_research_support import Provider, response
from tests.research_material_provider import ResearchProvider
from tests.revision_providers import reply
from tests.source_acquisition_support import ARTICLE

QUESTION = dict(
    question="Which observation established the chronology?",
    why_needed="The connecting observation is missing.",
)


def completed(*, targeted=False):
    text = (
        json.dumps(
            dict(
                answer="The retained account.",
                limitations="",
                evidence=[
                    dict(
                        url="https://example.com/article", quote=ARTICLE[:80], relation="The original words."
                    )
                ],
            )
        )
        if targeted
        else REPORT
    )
    return response("completed", steps=[dict(type="model_output", content=[dict(type="text", text=text)])])


class MaterialProvider(ResearchProvider):
    def __init__(self, stage):
        super().__init__()
        self.request_stage, self.requested = stage, False

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        turn = int(body["metadata"].get("podgen_source_turn", 0))
        if not self.requested and stage == self.request_stage and (stage != "fact-review" or turn > 0):
            self.requested = True
            if stage in ("draft", "revise-1"):
                self.requests.append(body)
                return reply(body, dict(material_request=QUESTION))
            raw = super().submit(body, client_request_id=client_request_id)
            value = json.loads(raw.body)
            text = value["output"][0]["content"][0]
            review = json.loads(text["text"])
            anchors = review["audit"]["claim_checks"][0]["anchors"]
            review["research_requests"] = [
                dict(QUESTION, anchors=anchors),
                dict(question="Which source dates the trial?", why_needed="Check the date.", anchors=anchors),
            ]
            text["text"] = json.dumps(review)
            return WireReply(json.dumps(value).encode())
        return super().submit(body, client_request_id=client_request_id)


@pytest.mark.parametrize("stage", ["draft", "revise-1", "fact-review", "fact-review-1"])
def test_terminal_material_continuation_reuses_accepted_plan_and_bounded_revision_allowance(
    repo, scope, producer, monkeypatch, stage
):
    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    if stage.startswith("fact-review"):
        # Exercise a persisted terminal review outcome, as retained older runs
        # produce. Current tool handoffs have their separate native-path test.
        monkeypatch.setattr(
            "podgen.runtime.models.research_tool_gateway.structured_requests", lambda *args: []
        )
        from podgen.research.materials.orders import KEY

        create = EvaluationJournal.create_run

        def legacy_manifest(self, command, manifest):
            manifest = dict(manifest)
            manifest.pop(KEY, None)
            return create(self, command, manifest)

        monkeypatch.setattr(EvaluationJournal, "create_run", legacy_manifest)
    models = MaterialProvider(stage)
    runner.writer.provider_factory = lambda _: models
    hosted = Provider(completed(), response(), completed())
    runner.research_provider = hosted
    prepare(repo, scope[0], config)
    waiting = runner.run(wait=False)
    assert waiting["phase"] == "terminal_materials" and waiting["status"] == "waiting"
    progress = waiting["progress"]
    parent = UUID(progress["writing_run_id"])
    journal = EvaluationJournal(repo, scope[0])
    original = digest(journal.manifest(parent))
    plan_ref = export_plan_ref(journal, parent)
    original_script = progress["continuation_script_ref"]
    if stage == "draft":
        assert original_script is None
    else:
        assert original_script is not None
    assert not speech.calls and len(progress["research_jobs"]) == 2
    child = ResearchJournal(repo, scope[0], UUID(progress["research_jobs"][1]))
    assignment = child.get("manifest")["config"]["assignment"]["user_request"]
    assert QUESTION["question"] in assignment
    if stage.startswith("fact-review"):
        assert "Which source dates the trial?" in assignment
    before = len(models.requests)
    result = runner.run()
    assert result["status"] == "assembled"
    continuation = saved_command(repo, scope[0], UUID(result["progress"]["writing_run_id"]))
    assert continuation.material_parent_run_id == parent
    assert continuation.plan_mode == "fixed-plan" and continuation.fixed_plan_input_ref == plan_ref
    assert (
        continuation.initial_script_ref is None
        if original_script is None
        else continuation.initial_script_ref.model_dump(mode="json") == original_script
    )
    assert continuation.max_script_revisions == (1 if stage == "fact-review-1" else 2)
    assert digest(journal.manifest(parent)) == original
    assert len(result["progress"]["research_jobs"]) == 2
    assert [name for name, _ in hosted.calls] == ["submit", "submit", "get"]
    continued_stages = [body["metadata"]["podgen_stage"] for body in models.requests[before:]]
    assert not any(name.startswith("plan") for name in continued_stages)
    assert ("draft" in continued_stages) == (stage == "draft")
    assert result["progress"]["revisions_used"] == (2 if stage == "fact-review-1" else 1)


class ToolHandoffProvider(ResearchProvider):
    def __init__(self):
        super().__init__()
        self.requested = set()

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        turn = int(body["metadata"].get("podgen_source_turn", 0))
        if stage in ("plan", "fact-review") and turn == 1 and stage not in self.requested:
            self.requested.add(stage)
            self.requests.append(body)
            args = dict(question=f"Question for {stage}?", reason="Missing a dated observation.")
            definition = next(t for t in body["tools"] if t.get("name") == "request_research")
            if "anchors" in definition["parameters"]["properties"]:
                from podgen.runtime.evaluation.request_payload import request_payload

                script = request_payload(
                    dict(body, input=[item for item in body["input"] if item.get("role") == "user"])
                )["script"]
                args["anchors"] = [dict(utterance_index=0, quote=script["utterances"][0]["text"])]
            return WireReply(
                json.dumps(
                    dict(
                        id=f"resp_{stage}_handoff",
                        status="completed",
                        model=body["model"],
                        metadata=body["metadata"],
                        output=[
                            dict(
                                type="function_call",
                                status="completed",
                                name="request_research",
                                call_id=f"{stage}_question",
                                arguments=json.dumps(args),
                            )
                        ],
                        usage=dict(input_tokens=100, output_tokens=100),
                    )
                ).encode()
            )
        return super().submit(body, client_request_id=client_request_id)


def test_resolved_planning_handoff_does_not_mask_later_fact_handoff(repo, scope, producer, monkeypatch):
    config, runner, _, _, _, _ = setup_episode(repo, scope, producer, monkeypatch)
    config = config.model_copy(
        update=dict(settings=config.settings.model_copy(update=dict(max_research_jobs=3)))
    )
    models = ToolHandoffProvider()
    runner.writer.provider_factory = lambda _: models
    hosted = Provider(completed(), completed(), completed(targeted=True))
    runner.research_provider = hosted
    prepare(repo, scope[0], config)
    result = runner.run()
    assert result["status"] == "assembled" and models.requested == {"plan", "fact-review"}
    jobs = result["progress"]["research_jobs"]
    assert len(set(jobs)) == 3 and [name for name, _ in hosted.calls] == ["submit"] * 3
    questions = [
        ResearchJournal(repo, scope[0], UUID(run_id)).get("manifest")["config"]["assignment"]["user_request"]
        for run_id in jobs[1:]
    ]
    assert questions == ["Question for plan?", "Question for fact-review?"]
    assert len(result["progress"]["writing_runs"]) == 1
    assert sum(body["metadata"]["podgen_stage"] == "draft" for body in models.requests) == 1
