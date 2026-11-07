"""Offline provider replies exercise the normal runtime, never a separate application path."""

import json
from copy import deepcopy

from podgen.runtime.models.contracts import WireReply
from tests.revision_cases import actual_case, composition, corrections_83, scope
from tests.test_editorial_independence import payload
from tests.test_independent_readings import ReadingProvider


def reply(body, output):
    return WireReply(
        json.dumps(
            dict(
                id="resp_" + body["metadata"]["podgen_stage"].replace("-", "_"),
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
                usage=dict(input_tokens=100, output_tokens=100),
            ),
            ensure_ascii=False,
        ).encode()
    )


class Actual83Provider(ReadingProvider):
    def __init__(self, references):
        super().__init__(pause="revise-1")
        self.references = references

    def submit(self, body, *, client_request_id):
        self.requests.append(deepcopy(body))
        stage, data = body["metadata"]["podgen_stage"], payload(body)
        _, _, _, reviews = actual_case()
        if stage in ("reader-review", "fact-review"):
            return reply(
                body, self.references.translate(reviews["reader" if stage.startswith("reader") else "fact"])
            )
        if stage == "revise-1":
            edits = []
            for anchor in corrections_83():
                target = next(
                    t
                    for t in data["revision_operation"]["targets"]
                    if t.get("utterance_index") == anchor["utterance_index"]
                    and t.get("text") == anchor["quote"]
                )
                edits.append(dict(operation="replace", target_id=target["target_id"], text=anchor["text"]))
            return reply(body, dict(edits=edits, composition_update=None))
        return reply(
            body,
            dict(
                reading_mode="script_only" if stage.startswith("reader") else "informed",
                listener_answer="Synthetic fresh complete reading.",
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note="Synthetic unmeasured reading.",
                recommendation="keep",
                revision_scope=scope("none"),
            ),
        )


class ContinuingProvider(ReadingProvider):
    def __init__(self, first_mode):
        super().__init__(("revise", "revise", "keep"), pause="revise-2")
        self.first_mode = first_mode

    def submit(self, body, *, client_request_id):
        stage, data = body["metadata"]["podgen_stage"], payload(body)
        if stage == "draft":
            self.requests.append(deepcopy(body))
            speaker = data["assignment"]["speakers"][0]["label"]
            return reply(
                body,
                dict(
                    utterances=[
                        dict(speaker=speaker, text=text)
                        for text in (
                            "Opening intact.",
                            "First discovery.",
                            "Second discovery.",
                            "Dependent ending.",
                        )
                    ]
                ),
            )
        if stage == "revise-1":
            self.requests.append(deepcopy(body))
            rows = [dict(speaker=r["speaker"], text=r["text"]) for r in data["script"]["utterances"]]
            selected = [rows[2], rows[1]]
            if self.first_mode == "sequence":
                return reply(
                    body,
                    dict(
                        sequences=[dict(range_id="R1", utterances=selected)],
                        edits=[],
                        composition=composition("Chosen"),
                    ),
                )
            return reply(
                body,
                dict(
                    script=dict(utterances=[rows[0], *selected, rows[3]]), composition=composition("Chosen")
                ),
            )
        result = super().submit(body, client_request_id=client_request_id)
        if stage == "plan":
            envelope = json.loads(result.body)
            content = envelope["output"][0]["content"][0]
            plan = json.loads(content["text"])
            plan["research_requests"].append(
                dict(
                    question="Still open?",
                    why_needed="Reordering alone does not resolve research.",
                    material_refs=[],
                    affected_act_ids=[],
                    blocking=False,
                )
            )
            content["text"] = json.dumps(plan, ensure_ascii=False)
            return WireReply(json.dumps(envelope, ensure_ascii=False).encode())
        if stage in ("reader-review", "fact-review", "reader-review-1", "fact-review-1"):
            envelope = json.loads(result.body)
            content = envelope["output"][0]["content"][0]
            review = json.loads(content["text"])
            index = 1 if stage == "reader-review" else 2
            anchor = dict(utterance_index=index, quote=data["script"]["utterances"][index]["text"])
            for finding in review["findings"]:
                finding["anchors"] = [anchor]
            review["revision_scope"]["anchors"] = [anchor]
            if stage == "reader-review":
                review["strengths"] = [
                    dict(
                        anchors=[dict(utterance_index=0, quote="Opening intact.")],
                        reason="The opening should survive.",
                    )
                ]
                review["revision_scope"]["scale"] = (
                    "sequence" if self.first_mode == "sequence" else "whole_episode"
                )
                if self.first_mode == "sequence":
                    review["revision_scope"]["sequence_ranges"] = [
                        dict(
                            anchors=[
                                anchor,
                                dict(utterance_index=2, quote=data["script"]["utterances"][2]["text"]),
                            ],
                            reason="Reverse only the two discoveries.",
                        )
                    ]
                elif self.first_mode == "fact-structural":
                    review.update(recommendation="keep", findings=[], revision_scope=scope("none"))
            elif stage == "fact-review" and self.first_mode == "fact-structural":
                review["revision_scope"]["scale"] = "whole_episode"
            elif stage == "reader-review-1":
                review.update(
                    recommendation="keep",
                    findings=[],
                    revision_scope=scope("none", anchors=[dict(utterance_index=0, quote="Opening intact.")]),
                )
            elif stage == "fact-review-1":
                review["revision_scope"]["dependent_passages"] = [
                    dict(
                        reason="The ending depends on the correction.",
                        anchors=[dict(utterance_index=3, quote="Dependent ending.")],
                    )
                ]
            content["text"] = json.dumps(review, ensure_ascii=False)
            return WireReply(json.dumps(envelope, ensure_ascii=False).encode())
        return result
