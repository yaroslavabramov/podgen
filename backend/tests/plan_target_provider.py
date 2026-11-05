"""Synthetic independent judgments followed by an explicit address-selection response."""

import json

from podgen.runtime.models.contracts import WireReply
from tests.own_plan_provider import OwnPlanProvider


class PlanTargetProvider(OwnPlanProvider):
    def __init__(self, *, selection="valid", stage="plan-review", decisions=("accept", "accept")):
        super().__init__(decisions=decisions, nonblocking=True)
        self.selection, self.target_stage = selection, stage

    def submit(self, body, *, client_request_id):
        if body["text"]["format"]["name"] == "plan_target_choices":
            self.requests.append(body)
            if self.on_submit:
                self.on_submit()
            repair = json.loads(body["input"][-1]["content"][0]["text"])
            choices = []
            for entry in repair["invalid_targets"]:
                # Fixed synthetic model selections, not a backend prefix-rewrite policy.
                selected = (
                    "/limitations" if entry["target_id"].startswith("findings/") else "/plan/acts/0/title"
                )
                if self.selection == "null":
                    selected = None
                elif self.selection == "unknown":
                    selected = "/plan/limitations"
                choices.append(dict(target_id=entry["target_id"], selected_path=selected))
            if self.selection == "duplicate":
                choices.append(choices[0])
            if self.selection == "missing":
                choices.pop()
            result = dict(choices=choices)
            if self.selection == "change-decision":
                result["decision"] = "accept"
            response = dict(
                id="resp_target_choices",
                status="completed",
                model=body["model"],
                metadata=body["metadata"],
                output=[
                    dict(
                        type="message",
                        role="assistant",
                        content=[dict(type="output_text", text=json.dumps(result))],
                    )
                ],
                usage=dict(input_tokens=100, output_tokens=123),
            )
            self.last = response
            return WireReply(json.dumps(response).encode())
        reply = super().submit(body, client_request_id=client_request_id)
        if body["metadata"]["podgen_stage"] == self.target_stage:
            response = json.loads(reply.body)
            content = response["output"][0]["content"][0]
            review = json.loads(content["text"])
            review["strengths"].append(
                dict(
                    targets=["/candidate_plan/plan/acts/0/title", "/plan/route_summary"],
                    reason="SYNTHETIC useful act and valid route.",
                )
            )
            review["findings"].append(
                dict(
                    targets=["/candidate_plan/plan/limitations"],
                    priority="optional",
                    observation="SYNTHETIC preserve stated limits.",
                    requested_change="SYNTHETIC keep limits audible.",
                    preserve=["SYNTHETIC qualified conclusion."],
                    material_refs=[],
                )
            )
            content["text"] = json.dumps(review, ensure_ascii=False)
            self.last = response
            return WireReply(json.dumps(response, ensure_ascii=False).encode())
        return reply
