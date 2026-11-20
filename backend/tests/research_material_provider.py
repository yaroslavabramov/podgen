"""Full production workflow double, including independent report/source reads."""

import json
from copy import deepcopy

from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.models.contracts import WireReply
from tests.author_responses import author_response
from tests.own_plan_provider import proposal
from tests.reading_audit_cases import report
from tests.test_independent_readings import ReadingProvider


class ResearchProvider:
    def __init__(self):
        self.requests = []
        self.writer = ReadingProvider()

    def count(self, body):
        return WireReply(b'{"input_tokens":100}')

    def submit(self, body, *, client_request_id):
        self.requests.append(deepcopy(body))
        stage = body["metadata"]["podgen_stage"]
        if stage.startswith("revise-"):
            return self.writer.submit(body, client_request_id=client_request_id)
        data = request_payload(
            dict(body, input=[next(item for item in body["input"] if item.get("role") == "user")])
        )
        turn = int(body["metadata"].get("podgen_source_turn", 0))
        reads = stage.startswith("plan") or stage == "fact-review"
        if reads and turn == 0:
            output = [
                dict(
                    type="function_call",
                    call_id=f"{stage}_report",
                    name="read_material",
                    arguments=json.dumps(dict(material_id="M1", start=0, end=500, context=True)),
                    status="completed",
                ),
                dict(
                    type="function_call",
                    call_id=f"{stage}_source",
                    name="read_source",
                    arguments=json.dumps(dict(source_id="S11", start=0, end=150)),
                    status="completed",
                ),
            ]
        else:
            if stage in ("plan", "plan-revise"):
                value = proposal(
                    dict(
                        material_catalog=["M1"],
                        speaker_labels=[s["label"] for s in data["assignment"]["speakers"]],
                    ),
                    stage,
                )
                value.pop("limitations", None)
                for act in value["plan"]["acts"]:
                    act.pop("limitations_to_voice", None)
                value["plan"]["ending"] = dict(
                    last_moment="The investigation reaches its result.",
                    why_end_here="The promised account reaches its answer.",
                    promises_to_close=[],
                )
            elif stage.startswith("plan-review"):
                revise = stage == "plan-review"
                value = dict(
                    decision="revise" if revise else "accept",
                    decision_reason="Separate inspection of report and retained document.",
                    listener_answer="The account has a supported route.",
                    strengths=[],
                    findings=[
                        dict(
                            targets=["/plan/route_summary"],
                            priority="important",
                            observation="Clarify the route.",
                            requested_change="Connect the observations.",
                            preserve=[],
                            material_refs=["M1"],
                        )
                    ]
                    if revise
                    else [],
                    research_requests=[],
                    duration_note="No timed reading.",
                )
            elif stage == "draft":
                assert data["support"][0]["card_id"] == "M1"
                value = dict(
                    utterances=[
                        dict(
                            speaker=data["assignment"]["speakers"][0]["label"],
                            text="The investigation followed the water. The conclusion concerns this investigation.",
                        )
                    ]
                )
            else:
                fact = stage.startswith("fact-review")
                context = data["audit_context"]
                script = dict(
                    utterances=[
                        {k: v for k, v in row.items() if k != "index"} for row in data["script"]["utterances"]
                    ]
                )
                value = report(
                    script,
                    fact=fact,
                    context=context,
                    required=fact and stage == "fact-review",
                    source_id="E2" if context["phase"] == "first" else "H1",
                    source_review="current" if context["phase"] == "first" else "previous",
                )
                if fact and value["recommendation"] == "keep":
                    # This fixture checks one scoped statement, with no causal,
                    # chronological or population-wide claims to leave unfinished.
                    for area in value["audit"]["areas"]:
                        if area["status"] == "unchecked":
                            area.update(
                                status="not_applicable",
                                explanation="No such claim is asserted by this synthetic scoped account.",
                            )
                if fact and value["findings"]:
                    value["findings"][0]["support_labels"] = ["M1"]
            from jsonschema import Draft202012Validator

            serialized = author_response(body, value)
            Draft202012Validator(body["text"]["format"]["schema"]).validate(json.loads(serialized))
            output = [
                dict(
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text=serialized)],
                )
            ]
        return WireReply(
            json.dumps(
                dict(
                    id="resp_" + str(len(self.requests)),
                    status="completed",
                    model=body["model"],
                    metadata=body["metadata"],
                    output=output,
                    usage=dict(input_tokens=100, output_tokens=100),
                ),
                ensure_ascii=False,
            ).encode()
        )
