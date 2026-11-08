"""Local double; capture the actual wire before independently choosing a different plot."""

import json
from copy import deepcopy

from podgen.runtime.models.contracts import WireReply
from tests.own_plan_provider import proposal


class WireCaptured(Exception):
    pass


class IndependentPlannerProvider:
    def __init__(self, *, capture_only=False, review_decision="keep"):
        self.requests = []
        self.pre_generation = None
        self.plan = None
        self.script = None
        self.revised_script = None
        self.feedback = None
        self.review_decision = review_decision
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
            if "PlanningFinalMoment" in body["text"]["format"]["schema"].get("$defs", {}):
                own["working_answer"] = own["ending"]["answer_to_deliver"]
                own["ending"] = dict(
                    last_moment="Синтетический последний момент: наблюдение, после которого рассказ останавливается.",
                    why_end_here="Этот момент завершает выбранное обещание без повторного объяснения.",
                    promises_to_close=[],
                )
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
                    dict(
                        speaker=s["label"],
                        text=(
                            "Мы наблюдаем несколько связанных величин. Сначала разберём, что именно они измеряют."
                            if index == 0
                            else "Тогда проверим допущение: как изменятся наблюдения, если меняется только одна неизвестная?"
                        ),
                    )
                    for index, s in enumerate(payload["assignment"]["speakers"])
                ]
            )
            self.script = deepcopy(output)
        elif stage == "review":
            output = dict(
                reading_mode="informed",
                recommendation=self.review_decision,
                listener_answer="Собственный ответ сохранён.",
                important_limits=[],
                strengths=[],
                findings=[],
                research_requests=[],
                duration_note="Читка не проводилась.",
            )
            if self.review_decision == "revise":
                output["findings"] = [
                    dict(
                        dimension="ending",
                        priority="important",
                        anchors=[dict(utterance_index=0, quote=self.script["utterances"][0]["text"])],
                        observation="Вывод не вытекает из показанного наблюдения.",
                        requested_change="Связать вывод с наблюдением в реальном сценарии.",
                        preserve=["Предмет вопроса."],
                    )
                ]
            self.feedback = deepcopy(output)
        elif stage == "revise":
            output = dict(
                utterances=[
                    dict(
                        speaker=s["label"],
                        text=(
                            "Наблюдения относятся к одной задаче. Поэтому мы сопоставляем их вместе и выводим результат из этого сравнения."
                            if index == 0
                            else "Значит, вывод следует из совместной проверки. Проверим и условия, при которых он остаётся верен."
                        ),
                    )
                    for index, s in enumerate(payload["assignment"]["speakers"])
                ]
            )
            self.revised_script = deepcopy(output)
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
