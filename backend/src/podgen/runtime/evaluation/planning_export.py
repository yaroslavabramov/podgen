"""Compact readable evaluation plan; exact machine output remains in the adjacent JSON archives."""

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import one

from .material_names import display_materials, frozen_names
from .planning import clearance_ref, verify_clearance

RU = {
    "title": "План выпуска",
    "unaccepted": "План-кандидат: редакционная проверка не пройдена",
    "research": "Для плана нужен материал",
    "accepted": "Редактор плана допустил эту версию к автору.",
    "evaluation": "Оценочный кандидат; не является принятым производственным StoryPlan.",
    "story_selection": "Выбор рассказа",
    "first": "Первый замысел",
    "second": "Второй замысел",
    "difference": "Существенное различие",
    "selected": "Выбранный вариант",
    "why_selected": "Что даёт этот выбор",
    "question": "Главный вопрос",
    "promise": "Обещание слушателю",
    "answer": "Рабочий ответ",
    "route": "Маршрут",
    "opening": "Открытие",
    "approach": "Подход",
    "opening_question": "Вопрос открытия",
    "refs": "Опоры",
    "acts": "Акты",
    "development": "Ход сцены",
    "editorial_notes": "Редакторские заметки",
    "claim_scope": "Основание утверждения",
    "editorial_choice": "Редакторский выбор",
    "act_question": "Вопрос акта",
    "change": "Что поймёт слушатель",
    "prerequisites": "Что нужно объяснить раньше",
    "voice_limits": "Ограничения для озвучивания",
    "previous": "Переход от предыдущего",
    "next": "Переход к следующему",
    "opened": "Новые обещания",
    "addressed": "Закрываемые обещания",
    "time": "Оценка времени",
    "essential": "основной",
    "optional": "дополнительный",
    "ending": "Финал",
    "ending_answer": "Ответ в финале",
    "last_moment": "Последний момент",
    "why_end_here": "Почему закончить здесь",
    "qualifications": "Оговорки",
    "close": "Закрыть обещания",
    "hosts": "Ведущие",
    "contribution": "Вклад",
    "interaction": "Взаимодействие",
    "register": "Речь",
    "duration": "Длительность",
    "assumptions": "Допущения",
    "optional_material": "Что можно сократить",
    "limits": "Ограничения",
    "questions": "Вопросы к исследованию",
    "blocking": "Блокирующий вопрос",
    "nonblocking": "Неблокирующий вопрос",
    "why": "Почему нужен ответ",
    "affected": "Затронутые акты",
    "decision": "Решение редактора",
    "reason": "Причина",
    "reconstructed": "Как редактор понял замысел",
    "duration_note": "Замечание о длительности",
    "accept": "допустить к автору",
    "revise": "правка",
    "needs_research": "нужен материал",
    "seconds": "с",
}
EN = {key: key.replace("_", " ").capitalize() for key in RU} | {
    "title": "Episode plan",
    "unaccepted": "Plan candidate: planning review has not passed",
    "research": "Material needed for the plan",
    "accepted": "The plan editor cleared this version for writing.",
    "evaluation": "Evaluation candidate; not an accepted production StoryPlan.",
    "seconds": "seconds",
    "change": "Intended change in understanding",
    "previous": "Transition from previous act",
    "next": "Transition to next act",
    "refs": "Supporting material",
    "why": "Why the answer is needed",
}


def render_plan(candidate, review, accepted, language, *, material_titles=None):
    labels = RU if language.startswith("ru") else EN
    output = candidate["output"]
    plan = output["plan"]
    requests = list(output["research_requests"])
    if review:
        for item in review["output"]["research_requests"]:
            if item not in requests:
                requests.append(item)
    blocked = any(r["blocking"] for r in requests)
    title = (
        labels["title"]
        if accepted
        else labels["research"]
        if blocked or plan is None
        else labels["unaccepted"]
    )
    lines = ["# " + title, "", labels["accepted"] if accepted else labels["unaccepted"], labels["evaluation"]]

    def section(key):
        lines.extend(["", "## " + labels[key]])

    def field(key, value):
        if value is not None and value != "" and value != []:
            if isinstance(value, list):
                lines.append("**" + labels[key] + ":**")
                lines.extend("- " + str(v) for v in value)
            else:
                lines.append("**" + labels[key] + ":** " + str(value))

    def material_names(refs):
        return display_materials(refs, material_titles)

    def seconds(value):
        return f"{value['min_seconds']}–{value['max_seconds']} {labels['seconds']}"

    choice = output.get("story_selection")
    if choice is not None:
        section("story_selection")
        for key in ("first", "second", "difference"):
            field(key, choice[key])
        field("selected", labels[choice["selected"]])
        field("why_selected", choice["why_selected"])

    if plan:
        section("question")
        field("question", plan["main_question"])
        field("promise", plan["listener_promise"])
        field("answer", plan["working_answer"])
        if "editorial_notes" in output:
            from .scene_plan_export import render_scenes

            render_scenes(
                plan, output["editorial_notes"], lines, labels, section, field, material_names, seconds
            )
        else:
            section("route")
            lines.append(plan["route_summary"])
            section("opening")
            field("approach", plan["opening"]["approach"])
            field("opening_question", plan["opening"]["question_to_open"])
            field("refs", material_names(plan["opening"]["material_refs"]))
            section("acts")
            for index, act in enumerate(plan["acts"], 1):
                lines.extend(["", f"### {index}. {act['title']}"])
                for label, key in [
                    ("act_question", "listener_question"),
                    ("change", "intended_change_in_understanding"),
                    ("prerequisites", "prerequisite_concepts"),
                    ("voice_limits", "limitations_to_voice"),
                ]:
                    field(label, act.get(key))
                field("refs", material_names(act["material_refs"]))
                field("previous", act["transitions"]["from_previous"])
                field("next", act["transitions"]["toward_next"])
                field("opened", act["promises_opened"])
                field("addressed", act["promises_addressed"])
                field("time", seconds(act["estimated_seconds_range"]) + "; " + labels[act["priority"]])
            section("ending")
            if "last_moment" in plan["ending"]:
                field("last_moment", plan["ending"]["last_moment"])
                field("why_end_here", plan["ending"]["why_end_here"])
            else:
                field("ending_answer", plan["ending"]["answer_to_deliver"])
            field("qualifications", plan["ending"].get("qualifications"))
            field("close", plan["ending"]["promises_to_close"])
        section("hosts")
        for host in plan["host_functions"]:
            lines.append("**" + host["speaker_id"] + "**")
            field("contribution", host["contribution"])
            field("interaction", host["interaction_guidance"])
            field("register", host["speech_register"])
        section("duration")
        field("time", seconds(plan["duration_strategy"]["estimated_seconds_range"]))
        field("assumptions", plan["duration_strategy"]["assumptions"])
        field("optional_material", plan["duration_strategy"]["optional_material"])
    if output.get("limitations"):
        section("limits")
        lines.extend("- " + item for item in output["limitations"])
    if requests:
        section("questions")
        for request in requests:
            field("blocking" if request["blocking"] else "nonblocking", request["question"])
            field("why", request["why_needed"])
            field("affected", ", ".join(request["affected_act_ids"]))
            field("refs", material_names(request["material_refs"]))
    if review:
        section("decision")
        decision = review["output"]
        lines.append(labels[decision["decision"]])
        field("reason", decision["decision_reason"])
        field("reconstructed", decision["listener_answer"])
        field("limits", decision.get("important_limits"))
        field("duration_note", decision["duration_note"])
    return "\n".join(lines) + "\n"


def export_planning(journal, run_id, *, language):
    with journal.repo.database.transaction() as tx:
        journal.authorize(tx)
        present = one(
            tx,
            "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=:r",
            p=journal.project,
            r=clearance_ref(run_id).record_id,
        )
    proof = verify_clearance(journal, run_id) if present else None
    if proof:
        candidate = journal.read(RuntimeRef.model_validate(proof["candidate_plan_ref"]))
        review = journal.read(RuntimeRef.model_validate(proof["plan_review_ref"]))
    else:
        plans = [journal.invocation(run_id, s) for s in ("plan", "plan-revise")]
        selected = next((p for p in reversed(plans) if p and p["status"] == "completed"), None)
        if selected is None:
            if journal.manifest(run_id).get("planning_continuation"):
                from .planning_continuation.export import seed_records

                candidate, review = seed_records(journal, journal.manifest(run_id))
                rendered = render_plan(
                    candidate,
                    review,
                    False,
                    language,
                    material_titles=frozen_names(journal, run_id, journal.manifest(run_id)),
                )
                return rendered, None
            return None, None
        candidate = journal.output(selected)
        review = None
        for stage in ("plan-review-revised", "plan-review"):
            invocation = journal.invocation(run_id, stage)
            if invocation and invocation["status"] == "completed":
                record = journal.output(invocation)
                if record["candidate_plan_ref"]["record_id"] == str(selected["output_record_id"]):
                    review = record
                    break
    material_titles = frozen_names(journal, run_id, journal.manifest(run_id))
    rendered = render_plan(candidate, review, proof is not None, language, material_titles=material_titles)
    if proof and proof.get("additional_conditions"):
        rendered += (
            "\nАдреса отзыва исправлены отдельной модельной попыткой; это дополнительное условие оценки.\n"
            if language.startswith("ru")
            else "\nReview addresses were repaired in a separate model call: an additional evaluation condition.\n"
        )
    return rendered, proof
