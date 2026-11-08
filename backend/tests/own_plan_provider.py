"""Controlled provider double: independent synthetic plan, explicit review and a changed revision."""

import json

from podgen.runtime.models.contracts import ProviderFault, WireReply
from tests.planning_choice_cases import answer_requested_selection
from tests.test_writing_evaluation import FakeProvider


def proposal(materials, stage):
    text = f"SYNTHETIC OWN PLAN {stage}: explain the evidence and its limits."
    refs = [materials["material_catalog"][-1]]
    seconds = dict(min_seconds=30, max_seconds=90)
    return dict(
        status="ready",
        plan=dict(
            main_question=text,
            listener_promise=text,
            working_answer=text,
            route_summary=text,
            opening=dict(approach=text, material_refs=refs, question_to_open=text),
            acts=[
                dict(
                    act_id="a1",
                    title=text,
                    listener_question=text,
                    intended_change_in_understanding=text,
                    prerequisite_concepts=[],
                    material_refs=refs,
                    limitations_to_voice=[text],
                    transitions=dict(from_previous=text, toward_next=text),
                    promises_opened=[],
                    promises_addressed=[],
                    estimated_seconds_range=seconds,
                    priority="essential",
                )
            ],
            ending=dict(answer_to_deliver=text, qualifications=[text], promises_to_close=[]),
            host_functions=[
                dict(speaker_id=s, contribution=text, interaction_guidance=text, speech_register=text)
                for s in materials["speaker_labels"]
            ],
            duration_strategy=dict(estimated_seconds_range=seconds, assumptions=[text], optional_material=[]),
        ),
        limitations=[text],
        research_requests=[],
    )


def question(blocking=True):
    return dict(
        question="SYNTHETIC missing observation?",
        why_needed="SYNTHETIC required support.",
        affected_act_ids=[],
        material_refs=[],
        blocking=blocking,
    )


class OwnPlanProvider(FakeProvider):
    def __init__(
        self,
        *,
        decisions=("revise", "accept"),
        plan_deficit=False,
        fault_stage=None,
        plan_fault=None,
        nonblocking=False,
    ):
        super().__init__()
        self.decisions, self.plan_deficit = decisions, plan_deficit
        self.fault_stage, self.plan_fault, self.nonblocking = fault_stage, plan_fault, nonblocking

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        if not stage.startswith("plan"):
            return super().submit(body, client_request_id=client_request_id)
        self.requests.append(body)
        if self.on_submit:
            self.on_submit()
        fault = self.plan_fault if stage == self.fault_stage else None
        if fault == "timeout":
            raise ProviderFault("provider_transport_outcome_unknown", ambiguous=True)
        payload = json.loads(body["input"][0]["content"][0]["text"])
        materials = payload["author_materials"]
        if stage in ("plan", "plan-revise"):
            output = proposal(materials, stage)
            if self.plan_deficit:
                output.update(status="needs_research", plan=None, research_requests=[question()])
            elif self.nonblocking:
                output["research_requests"] = [question(False)]
            if fault == "locator":
                output["plan"]["acts"][0]["material_refs"] = ["author/gps-clock/plan.md"]
        else:
            decision = self.decisions[0 if stage == "plan-review" else 1]
            output = dict(
                decision=decision,
                decision_reason="SYNTHETIC separate decision.",
                listener_answer="SYNTHETIC inferred answer.",
                important_limits=[],
                strengths=[dict(targets=["/plan/route_summary"], reason="SYNTHETIC useful route.")],
                findings=[
                    dict(
                        targets=["/plan/acts/0/transitions/toward_next"],
                        priority="important",
                        observation="SYNTHETIC missing link.",
                        requested_change="SYNTHETIC explain link.",
                        preserve=[],
                        material_refs=[materials["material_catalog"][-1]],
                    )
                ]
                if decision == "revise"
                else [],
                research_requests=[question()]
                if decision == "needs_research" or fault == "accept-blocked"
                else ([question(False)] if self.nonblocking else []),
                duration_note="SYNTHETIC estimate only.",
            )
            if fault == "pointer":
                output["strengths"][0]["targets"] = ["/plan/acts/99"]
        output = answer_requested_selection(output, body)
        response = dict(
            id="resp_" + stage.replace("-", "_"),
            status="completed",
            model=body["model"],
            metadata=body["metadata"],
            output=[
                dict(
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text=json.dumps(output, ensure_ascii=False))],
                )
            ],
            usage=dict(input_tokens=100, output_tokens=123),
        )
        if fault == "incomplete":
            response.update(status="incomplete", incomplete_details=dict(reason="max_output_tokens"))
        if fault == "model":
            response["model"] = "wrong-model"
        if fault == "invalid-json":
            response["output"][0]["content"][0]["text"] = "{broken"
        self.last = response
        return WireReply(json.dumps(response, ensure_ascii=False).encode())
