"""Local double; capture the actual wire before independently choosing a different plot."""

import json
from copy import deepcopy

from podgen.runtime.models.contracts import WireReply
from tests.own_plan_provider import proposal


class WireCaptured(Exception):
    pass


class IndependentPlannerProvider:
    def __init__(self, *, capture_only=False):
        self.requests = []
        self.pre_generation = None
        self.plan = None
        self.capture_only = capture_only

    def count(self, body):
        if body["text"]["format"]["name"] == "plan":
            assert self.plan is None
            self.pre_generation = deepcopy(body)
            if self.capture_only:
                raise WireCaptured()
        return WireReply(b'{"input_tokens":100}')

    def submit(self, body, *, client_request_id):
        self.requests.append(deepcopy(body))
        payload = json.loads(body["input"][0]["content"][0]["text"])
        stage = body["metadata"]["podgen_stage"]
        if stage == "plan":
            self.plan = proposal(
                dict(
                    material_catalog=payload["material_catalog"],
                    speaker_labels=[s["label"] for s in payload["assignment"]["speakers"]],
                ),
                "independent-response",
            )
            own = self.plan["plan"]
            own["opening"]["approach"] = (
                "СОБСТВЕННЫЙ МАРШРУТ: начать с неизвестных величин и проверить наблюдения."
            )
            own["ending"]["answer_to_deliver"] = "СОБСТВЕННЫЙ ОТВЕТ, ВЫБРАННЫЙ ЛОКАЛЬНЫМ ПРОВАЙДЕРОМ."
            own["duration_strategy"]["estimated_seconds_range"] = dict(min_seconds=780, max_seconds=1080)
            output = self.plan
        elif stage == "plan-review":
            output = dict(
                decision="accept",
                decision_reason="Отдельная локальная проверка собственной версии.",
                listener_answer="Проверен собственный маршрут.",
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note="Читка не проводилась.",
            )
        elif stage == "draft":
            output = dict(
                utterances=[
                    dict(speaker=s["label"], text=payload["story"]["ending"]["answer_to_deliver"])
                    for s in payload["assignment"]["speakers"]
                ]
            )
        elif stage == "review":
            output = dict(
                reading_mode="informed",
                recommendation="keep",
                listener_answer="Собственный ответ сохранён.",
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note="Читка не проводилась.",
            )
        else:
            raise AssertionError(f"Unexpected paid stage: {stage}")
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
                            content=[dict(type="output_text", text=json.dumps(output, ensure_ascii=False))],
                        )
                    ],
                    usage=dict(input_tokens=100, output_tokens=100),
                ),
                ensure_ascii=False,
            ).encode()
        )
