"""Native provider double: two advances, a final reader keep and one outstanding fact repair."""

import json
from copy import deepcopy

from podgen.runtime.models.contracts import WireReply

from tests.revision_cases import scope
from tests.test_editorial_independence import payload
from tests.test_independent_readings import ReadingProvider
from tests.test_revision_verification_runtime import CheckingProvider


class EditorialProvider(CheckingProvider):
    def __init__(self):
        super().__init__(("revise", "revise", "keep"))
        self.continuing = False
        self.reject_fact = False
        self.legacy = False

    def submit(self, body, *, client_request_id):
        stage = body["metadata"]["podgen_stage"]
        if not stage.startswith("fact-review"):
            if self.continuing:
                self.decisions = ("keep",)
            reply = (
                ReadingProvider.submit(self, body, client_request_id=client_request_id)
                if self.legacy
                else super().submit(body, client_request_id=client_request_id)
            )
            if self.continuing and stage.startswith("reader-review"):
                envelope = json.loads(reply.body)
                content = envelope["output"][0]["content"][0]
                review = json.loads(content["text"])
                review["revision_scope"] = scope("none", anchors=[])
                content["text"] = json.dumps(review)
                reply = WireReply(json.dumps(envelope).encode())
            return reply
        self.requests.append(deepcopy(body))
        data = payload(body)
        required = not self.continuing or self.reject_fact
        anchor = dict(utterance_index=0, quote=data["script"]["utterances"][0]["text"])
        finding = dict(
            dimension="factuality",
            priority="important",
            anchors=[anchor],
            observation="The source permits a narrower final claim.",
            requested_change="Limit the final claim to this investigation.",
            preserve=["The established observations."],
            support_labels=[],
        )
        if "SourcedFactFinding" in body["text"]["format"]["schema"].get("$defs", {}):
            finding["source_read_ids"] = []
        review = dict(
            reading_mode="informed",
            listener_answer="The supported account has been checked.",
            important_limits=[],
            strengths=[],
            findings=[finding] if required else [],
            research_requests=[],
            duration_note="No timed reading.",
            recommendation="revise" if required else "keep",
            revision_scope=scope("local" if required else "none", anchors=[anchor] if required else []),
        )
        return WireReply(
            json.dumps(
                dict(
                    id=f"resp_{len(self.requests)}",
                    model=body["model"],
                    metadata=body["metadata"],
                    status="completed",
                    output=[
                        dict(
                            type="message",
                            role="assistant",
                            content=[dict(type="output_text", text=json.dumps(review))],
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                )
            ).encode()
        )
