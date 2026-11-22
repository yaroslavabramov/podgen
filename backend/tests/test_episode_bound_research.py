"""Native episode recovery retains exact requests and never repays completed research."""

import json
from copy import deepcopy
from uuid import UUID

import pytest

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.episodes.production.prepare import prepare
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.materials.orders import KEY, VERSION
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.contracts import WireReply
from tests.episode_production_support import setup_episode
from tests.hosted_research_support import Provider, response
from tests.research_material_provider import ResearchProvider
from tests.revision_providers import reply
from tests.test_episode_material_continuation import completed

SCRIPT = ["The investigation begins here.", "Согласный на письме один и тот же.", "The account ends here."]


def questions():
    anchors = [dict(utterance_index=1, quote=SCRIPT[1])]
    return [
        dict(
            question="Verify the exact comparison.",
            why_needed="Check its contextual meaning.",
            anchors=[*anchors, dict(utterance_index=0, quote=SCRIPT[0])],
        ),
        dict(
            question="Which original record establishes the date?",
            why_needed="The date is still unknown.",
            anchors=anchors,
        ),
    ]


class BoundProvider(ResearchProvider):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.request_run = None

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        turn = int(body["metadata"].get("podgen_source_turn", 0))
        if stage == "fact-review" and self.request_run is None:
            self.request_run = body["metadata"]["podgen_run_id"]
        this_run = body["metadata"].get("podgen_run_id") == self.request_run
        if stage == "draft":
            self.requests.append(deepcopy(body))
            return reply(body, dict(utterances=[dict(speaker="РАССКАЗЧИК", text=text) for text in SCRIPT]))
        if stage == "fact-review" and turn == 1 and self.mode == "tool":
            self.requests.append(deepcopy(body))
            request = questions()[0]
            return WireReply(
                json.dumps(
                    dict(
                        id="resp_bound_tool",
                        status="completed",
                        model=body["model"],
                        metadata=body["metadata"],
                        output=[
                            dict(
                                type="function_call",
                                call_id="bound_question",
                                name="request_research",
                                status="completed",
                                arguments=json.dumps(
                                    dict(
                                        question=request["question"],
                                        reason=request["why_needed"],
                                        anchors=request["anchors"],
                                    )
                                ),
                            )
                        ],
                        usage=dict(input_tokens=100, output_tokens=100),
                    )
                ).encode()
            )
        raw = super().submit(body, client_request_id=client_request_id)
        if stage == "fact-review" and turn > 0 and this_run:
            value = json.loads(raw.body)
            part = value["output"][0]["content"][0]
            review = json.loads(part["text"])
            review["recommendation"] = "needs_material"
            review["research_requests"] = questions()
            if turn > 1:
                rows = review["research_requests"][0]["anchors"]
                review["research_requests"][0]["anchors"] = [*reversed(rows), rows[0]]
                review["research_requests"][0]["why_needed"] = (
                    "The completed answer still needs contextual assessment."
                )
            part["text"] = json.dumps(review)
            return WireReply(json.dumps(value).encode())
        return raw


def unresolved():
    text = json.dumps(
        dict(
            answer="No date established.", limitations="The required original record is missing.", evidence=[]
        )
    )
    return response("completed", steps=[dict(type="model_output", content=[dict(type="text", text=text)])])


@pytest.mark.parametrize("mode", ["tool", "structured", "terminal"])
def test_episode_shares_bound_orders_preserves_remaining_question_and_recovers(
    repo, scope, producer, monkeypatch, mode
):
    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    config = config.model_copy(
        update=dict(settings=config.settings.model_copy(update=dict(max_research_jobs=3)))
    )
    models = BoundProvider(mode)
    runner.writer.provider_factory = lambda _: models
    hosted = Provider(completed(), completed(targeted=True), unresolved())
    runner.research_provider = hosted
    if mode == "terminal":
        monkeypatch.setattr("podgen.runtime.models.research_tool_gateway.structured_requests", lambda *a: [])
    prepare(repo, scope[0], config)
    if mode == "terminal":

        class Interrupted(BaseException):
            pass

        save = runner._save
        interrupted = False

        def before_checkpoint(phase, status="running"):
            nonlocal interrupted
            if (
                phase == "terminal_materials"
                and runner.progress.get("material_question_index") == 1
                and not interrupted
            ):
                interrupted = True
                raise Interrupted()
            return save(phase, status)

        monkeypatch.setattr(runner, "_save", before_checkpoint)
        with pytest.raises(Interrupted):
            runner.run()
        assert runner.journal.read()["progress"]["material_question_index"] == 0
        assert len(hosted.calls) == 2
    with pytest.raises(ContractError, match="episode_terminal_research_unresolved"):
        runner.run()
    saved = runner.journal.read()
    progress = saved["progress"]
    assert saved["status"] == "needs_attention" and saved["phase"] == "terminal_materials"
    assert progress["material_question_index"] == 1
    assert len(progress["material_questions"]) == 2
    assert len(progress["research_jobs"]) == 3
    assert [name for name, _ in hosted.calls] == ["submit"] * 3
    assert len(progress["writing_runs"]) == 1 and not speech.calls
    run = UUID(progress["writing_run_id"])
    journal = EvaluationJournal(repo, scope[0])
    manifest = journal.manifest(run)
    assert manifest[KEY] == VERSION
    results = progress["material_question_results"][str(run)]
    assert results["0"]["status"] == "resolved" and results["1"]["status"] == "unresolved"
    assert results["1"]["report_id"].startswith("X")
    assert results["1"]["limitations"] == "The required original record is missing."
    assert results["0"]["research_order_id"] != results["1"]["research_order_id"]
    for child_id, request in zip(progress["research_jobs"][1:], questions(), strict=True):
        child = ResearchJournal(repo, scope[0], UUID(child_id))
        commission = child.get("manifest")["config"]
        assert commission["assignment"]["user_request"] == request["question"]
        context = commission["assignment"]["script_context"]
        assert context["anchors"] == request["anchors"]
        assert [u["text"] for u in context["utterances"]] == SCRIPT
        assert "episode_assignment" not in commission["assignment"]
    before = (len(hosted.calls), len(models.requests), digest(manifest))
    with pytest.raises(ContractError, match="episode_terminal_research_unresolved"):
        runner.run()
    assert before == (len(hosted.calls), len(models.requests), digest(journal.manifest(run)))
    assert runner.journal.read()["progress"]["material_question_index"] == 1
    # Native requests and the terminal review resolve the same order identity.
    root = journal.invocation(run, "fact-review")
    identities = set()
    for event in journal.events(root):
        if event["event_kind"] == "source_tool_result":
            from podgen.domain.contracts.common import RuntimeRef

            result = journal.read(RuntimeRef(record_kind="tool_execution", record_id=event["record_id"]))[
                "data"
            ]["result"]
            if result.get("research_order_id"):
                identities.add(result["research_order_id"])
    if mode != "terminal":
        assert identities == {results["0"]["research_order_id"], results["1"]["research_order_id"]}


def test_complete_terminal_materials_resume_the_same_script_before_author_revision(
    repo, scope, producer, monkeypatch
):
    config, runner, _, _, speech, _ = setup_episode(repo, scope, producer, monkeypatch)
    config = config.model_copy(
        update=dict(settings=config.settings.model_copy(update=dict(max_research_jobs=3)))
    )
    models = BoundProvider("terminal")
    runner.writer.provider_factory = lambda _: models
    plain = "The original gives the comparison in its discussion of the inscription."
    plain_response = response(
        "completed", steps=[dict(type="model_output", content=[dict(type="text", text=plain)])]
    )
    hosted = Provider(completed(), completed(targeted=True), plain_response)
    runner.research_provider = hosted
    monkeypatch.setattr("podgen.runtime.models.research_tool_gateway.structured_requests", lambda *a: [])
    prepare(repo, scope[0], config)
    result = runner.run()
    assert result["status"] == "assembled" and speech.calls
    progress = result["progress"]
    assert len(progress["writing_runs"]) == 2 and len(progress["research_jobs"]) == 3
    assert sum(body["metadata"]["podgen_stage"] == "draft" for body in models.requests) == 1
    journal = EvaluationJournal(repo, scope[0])
    command = journal.manifest(UUID(progress["writing_run_id"]))["command"]
    assert command["initial_script_ref"] == progress["continuation_script_ref"]
    results = progress["material_question_results"][progress["writing_runs"][0]]
    assert all(value["status"] == "resolved" for value in results.values())
    assert results["1"]["report_excerpt"]["text"] == plain
    assert results["1"]["read_report"]["result_id"] == results["1"]["report_id"]
    calls = len(hosted.calls), len(models.requests)
    assert runner.run()["status"] == "assembled"
    assert calls == (len(hosted.calls), len(models.requests))
