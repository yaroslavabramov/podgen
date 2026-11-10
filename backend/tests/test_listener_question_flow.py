"""Real GPS/Snow inputs, cached texts only, and a one-field question correction through ordinary CLI."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen import writing_cli
from podgen.runtime.models.contracts import WireReply
from podgen.writing.plan_views import candidate_view
from podgen.writing.projection_versions import CURRENT
from tests.author_responses import author_response
from tests.planning_choice_cases import answer_requested_selection
from tests.test_editorial_independence import invoke, payload, prepare
from tests.test_own_plan import own_dataset as own_dataset

ROOT = Path(__file__).resolve().parents[2]


def current_planner_reply(source):
    """A synthetic V9 planner chooses a new final moment while retaining the archived episode answer."""
    reply = candidate_view(source, projection_version=CURRENT)
    reply["plan"]["ending"] = dict(
        last_moment="Последний документ закрывают на строке с датой наблюдения.",
        why_end_here="Читатель уже может сопоставить дату с тем, что было известно героям.",
        promises_to_close=reply["plan"]["ending"]["promises_to_close"],
    )
    return reply


class QuestionFlowProvider:
    def __init__(self, initial, draft, question):
        self.initial = initial
        self.draft = draft
        self.question = question
        self.accepted = deepcopy(initial)
        if question is not None:
            self.accepted["plan"]["main_question"] = question
        self.requests = []

    def count(self, body):
        return WireReply(b'{"input_tokens":100}')

    def submit(self, body, *, client_request_id):
        self.requests.append(deepcopy(body))
        stage = body["metadata"]["podgen_stage"]
        if stage in ("plan", "plan-revise"):
            output = self.initial if stage == "plan" else self.accepted
        elif stage.startswith("plan-review"):
            change = self.question is not None and stage == "plan-review"
            output = dict(
                decision="revise" if change else "accept",
                decision_reason=self.initial["plan"]["listener_promise"],
                listener_answer=self.initial["plan"]["working_answer"],
                strengths=[],
                findings=[
                    dict(
                        targets=["/plan/main_question"],
                        priority="important",
                        observation=self.initial["plan"]["main_question"],
                        requested_change=self.question,
                        preserve=[],
                        material_refs=[],
                    )
                ]
                if change
                else [],
                research_requests=[],
                duration_note=self.initial["plan"]["duration_strategy"]["assumptions"][0],
            )
            if "important_limits" in body["text"]["format"]["schema"]["properties"]:
                output["important_limits"] = []
        elif stage == "draft":
            output = self.draft
        elif stage == "review":
            output = dict(
                reading_mode="informed",
                recommendation="keep",
                listener_answer=self.initial["plan"]["working_answer"],
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note=self.initial["plan"]["duration_strategy"]["assumptions"][0],
            )
        else:
            raise AssertionError("No rewrite or further generation authorized in this test")
        output = answer_requested_selection(output, body)
        return WireReply(
            json.dumps(
                dict(
                    id="resp_" + stage.replace("-", "_"),
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(
                            type="message",
                            role="assistant",
                            content=[dict(type="output_text", text=author_response(body, output))],
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                ),
                ensure_ascii=False,
            ).encode()
        )


@pytest.mark.parametrize(
    "case,question",
    [
        ("gps-clock", "Зачем навигатору измерять время, чтобы найти место?"),
        ("snow-pump", "Как оценить пользу решения, если улучшение началось до него?"),
        ("snow-pump", None),
    ],
)
def test_accepted_listener_question_reaches_author_and_review_without_replacing_brief(
    own_dataset, case, question, tmp_path, monkeypatch
):
    prefix, _, own, _, _ = prepare(own_dataset, case, tmp_path, monkeypatch)
    if case == "gps-clock":
        folder = ROOT / "evals/reports/2026-11-06-gps-clean-run"
        initial = json.loads((folder / "accepted-plan.json").read_text())["output"]
        draft = json.loads((folder / "draft.json").read_text())["output"]
    else:
        initial = json.loads((ROOT / f"evals/editorial-inputs/v1/{case}/fixed-plan.json").read_text())["plan"]
        draft = json.loads(
            (ROOT / "evals/reports/2026-11-05-planning/own-plan/snow-pump/draft.json").read_text()
        )["output"]
    before = json.dumps(initial, ensure_ascii=False)
    proposed = current_planner_reply(initial)
    provider = QuestionFlowProvider(proposed, draft, question)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(writing_cli, "OpenAIProvider", lambda *args, **kwargs: provider)
    assert invoke(prefix, "run", **own) == 0
    views = {body["metadata"]["podgen_stage"]: payload(body) for body in provider.requests}
    assignment = json.loads((tmp_path / "shared.json").read_text())["assignment"]
    for view in views.values():
        assert view["assignment"]["question"] == assignment["question"]
        assert view["assignment"]["audience"] == assignment["audience"]
    central = provider.accepted["plan"]["main_question"]
    assert views["draft"]["story"]["main_question"] == central
    opening = initial["plan"]["opening"]
    refs = {key: f"M{index}" for index, key in enumerate(views["plan"]["material_catalog"], 1)}
    assert views["draft"]["story"]["opening"] == {
        "approach": opening["approach"],
        "question_to_open": opening["question_to_open"],
        "support_refs": [refs[key] for key in opening["material_refs"]],
    }
    assert views["review"]["editorial_frame"]["main_question"] == central
    assert views["review"]["draft"]["utterances"][0]["text"] == draft["utterances"][0]["text"]
    assert views["plan-review"]["candidate_plan"] == proposed
    assert all(v["assignment"]["audience"] == assignment["audience"] for v in views.values())
    if question is not None:
        assert views["plan-revise"]["revision_guidance"]["findings"][0]["requested_change"] == question
        assert views["plan-review-revised"]["candidate_plan"] == candidate_view(
            provider.accepted, projection_version=CURRENT
        )
        restored = deepcopy(provider.accepted)
        restored["plan"]["main_question"] = initial["plan"]["main_question"]
        assert restored == proposed
    else:
        assert central == assignment["question"]  # Still explicit when the accepted wording is unchanged.
    if case == "gps-clock":
        assert (
            len({central, assignment["question"], views["draft"]["story"]["opening"]["question_to_open"]})
            == 3
        )
    assert json.loads((tmp_path / "export/final.json").read_text())["output"] == draft
    assert json.dumps(initial, ensure_ascii=False) == before
