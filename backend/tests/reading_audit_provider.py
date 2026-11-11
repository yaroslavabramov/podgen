"""Native tool/rewrite audit double; no model service is involved."""

import json
from copy import deepcopy

from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.models.contracts import WireReply

from tests.reading_audit_cases import report
from tests.test_independent_readings import ReadingProvider


class AuditProvider:
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
        semantic = dict(body, input=[item for item in body["input"] if item.get("role") == "user"])
        payload = request_payload(semantic)
        fact = stage.startswith("fact-review")
        context = payload["audit_context"]
        initial = context["phase"] == "first"
        if fact and initial and body["metadata"].get("podgen_source_turn", "0") == "0":
            output = [
                dict(
                    type="function_call",
                    call_id="initial_read",
                    name="read_source",
                    arguments=json.dumps(dict(source_id="S1", start=0, end=200)),
                    status="completed",
                )
            ]
        else:
            required = fact and stage != "fact-review-2"
            value = report(
                dict(
                    utterances=[
                        {k: v for k, v in row.items() if k != "index"}
                        for row in payload["script"]["utterances"]
                    ]
                ),
                fact=fact,
                context=context,
                required=required,
                source_id="E1" if initial else "H1",
                source_review="current" if initial else "previous",
                still_open=fact and stage == "fact-review-1",
            )
            output = [
                dict(
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text=json.dumps(value, ensure_ascii=False))],
                )
            ]
        return WireReply(
            json.dumps(
                dict(
                    id=f"resp_audit_{len(self.requests)}",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=output,
                    usage=dict(input_tokens=100, output_tokens=100),
                ),
                ensure_ascii=False,
            ).encode()
        )
