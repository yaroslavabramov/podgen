"""The readable companion is a plan, not a machine-key dump or a false clearance."""

from podgen.runtime.evaluation.planning_export import render_plan
from tests.own_plan_provider import proposal, question


def test_russian_readable_plan_is_compact_and_contains_the_complete_explanatory_route():
    plan = proposal(dict(material_catalog=["G1"], speaker_labels=["НИНА", "ИЛЬЯ"]), "plan-revise")
    candidate = dict(output=plan)
    text = render_plan(candidate, None, True, "ru")
    assert "Редактор плана допустил" in text
    for heading in (
        "Главный вопрос",
        "Рабочий ответ",
        "Маршрут",
        "Открытие",
        "Акты",
        "Переход к следующему",
        "Опоры",
        "Финал",
        "Ведущие",
        "Длительность",
    ):
        assert heading in text
    assert "G1" in text and "НИНА" in text and "ИЛЬЯ" in text
    assert "min_seconds" not in text and "host_functions" not in text
    lines = text.splitlines()
    assert len(lines) < 100
    assert sum(not line for line in lines) < len(lines) // 3
    assert "\n\n\n" not in text


def test_blocker_export_leads_with_material_needed_and_preserves_exact_question():
    blocked = dict(
        status="needs_research",
        plan=None,
        limitations=["SYNTHETIC missing evidence."],
        research_requests=[question()],
    )
    text = render_plan(dict(output=blocked), None, False, "ru")
    assert text.startswith("# Для плана нужен материал")
    assert blocked["research_requests"][0]["question"] in text
    assert blocked["research_requests"][0]["why_needed"] in text
    assert "допустил эту версию" not in text
