"""Offline plans and real journal fixtures for bounded continuation tests."""

import json
from copy import deepcopy

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.runtime.evaluation.contracts import RunFailure, WritingRunCommand
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply
from tests.own_plan_provider import proposal, question
from tests.test_editorial_independence import payload, prepare
from tests.test_independent_readings import Paused, ReadingProvider


class ContinuationProvider(ReadingProvider):
    def __init__(self, decision="revise", *, research=False, pause=None, on_plan=None):
        super().__init__(("keep",))
        self.plan_decision, self.research = decision, research
        self.pause_planning, self.on_plan = pause, on_plan

    def count(self, body):
        if self.pause_planning and body["text"]["format"]["name"] == self.pause_planning.replace("-", "_"):
            raise Paused()
        return super().count(body)

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        if not stage.startswith("plan"):
            return super().submit(body, client_request_id=client_request_id)
        self.requests.append(deepcopy(body))
        if self.on_plan:
            self.on_plan(stage)
        data = payload(body)
        if stage == "plan":
            result = proposal(
                dict(
                    material_catalog=data["material_catalog"],
                    speaker_labels=[s["label"] for s in data["assignment"]["speakers"]],
                ),
                stage,
            )
            result.pop("limitations")
            if "PlanningFinalMoment" in body["text"]["format"]["schema"].get("$defs", {}):
                result["plan"]["ending"] = dict(
                    last_moment="SYNTHETIC selected stopping point.",
                    why_end_here="SYNTHETIC promise completed without a recap.",
                    promises_to_close=[],
                )
            else:
                result["plan"]["ending"].pop("qualifications")
            for act in result["plan"]["acts"]:
                act.pop("limitations_to_voice")
        elif stage == "plan-revise":
            result = deepcopy(data["candidate_plan"])
            result["plan"]["route_summary"] += "\nSYNTHETIC additional editorial revision."
            if self.research:
                result.update(status="needs_research", research_requests=[question()])
        else:
            result = dict(
                decision=self.plan_decision,
                decision_reason="SYNTHETIC independent decision on this exact candidate.",
                listener_answer="SYNTHETIC assessment.",
                strengths=[],
                findings=[
                    dict(
                        targets=["/plan/route_summary"],
                        priority="important",
                        observation="SYNTHETIC unrepaired transition.",
                        requested_change="SYNTHETIC repair the transition.",
                        preserve=[],
                        material_refs=[],
                    )
                ]
                if self.plan_decision == "revise"
                else [],
                research_requests=[question()] if self.plan_decision == "needs_research" else [],
                duration_note="SYNTHETIC estimate.",
            )
        return WireReply(
            json.dumps(
                dict(
                    id="resp_" + stage,
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=[
                        dict(
                            type="message",
                            role="assistant",
                            content=[dict(type="output_text", text=json.dumps(result, ensure_ascii=False))],
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                ),
                ensure_ascii=False,
            ).encode()
        )


def failed_source(dataset, tmp_path, monkeypatch):
    repo, project, _, writer, planner = dataset
    _, _, _, _, ref = prepare(dataset, "snow-pump", tmp_path, monkeypatch)
    from uuid import uuid4

    command = WritingRunCommand(
        run_id=uuid4(),
        project_id=project,
        actor_id=repo.actor_id,
        case_id="snow-pump",
        author_ref=RuntimeRef.model_validate_json(ref.read_bytes()),
        writer_profile_ref=writer,
        reviewer_profile_ref=writer,
        planner_profile_ref=planner,
        plan_reviewer_profile_ref=planner,
        plan_mode="own-plan",
    )
    provider = ContinuationProvider()
    with pytest.raises(RunFailure, match="planning_revision_limit_exhausted"):
        WritingEvaluation(repo, lambda _: provider).run(command)
    return command, EvaluationJournal(repo, project), provider
