from __future__ import annotations

from app.generation_v2.model_authored_language import model_authored_language_error
from app.generation_v2.source_triage_language_contract import (
    question_resolution_language_error,
    source_analysis_language_error,
)


def test_unambiguously_english_authored_prose_is_rejected_for_russian_output() -> None:
    error = model_authored_language_error(
        output_language="ru",
        fields={
            "source_summary": (
                "This source explains the institutional mechanism entirely in English and does not contain "
                "any Russian model-authored prose at all."
            )
        },
    )

    assert error is not None
    assert error["error"] == "model_authored_prose_language_mismatch"
    assert error["invalid_fields"][0]["field"] == "source_summary"


def test_russian_prose_with_english_terms_is_not_rejected() -> None:
    assert model_authored_language_error(
        output_language="ru",
        fields={
            "statement": (
                "Источник описывает работу Exchange Fund и Certificates of Indebtedness, но не объясняет "
                "первоначальный правовой механизм."
            )
        },
    ) is None


def test_russian_sentence_with_an_english_clause_is_rejected() -> None:
    error = model_authored_language_error(
        output_language="ru",
        fields={
            "source_summary": (
                "Статья показывает, что Hong Kong maintained a currency board from 1935 to 1974, "
                "после чего режим изменился."
            )
        },
    )

    assert error is not None
    assert error["invalid_fields"][0]["foreign_lowercase_words"] == [
        "maintained",
        "currency",
        "board",
        "from",
    ]


def test_russian_prose_with_long_proper_name_sequence_is_not_rejected() -> None:
    assert model_authored_language_error(
        output_language="ru",
        fields={
            "source_summary": (
                "Документ выпустили Hong Kong Monetary Authority и Bank of England; "
                "основной вывод автор сформулировал по-русски."
            )
        },
    ) is None


def test_short_lowercase_units_do_not_trigger_the_prose_gate() -> None:
    assert model_authored_language_error(
        output_language="ru",
        fields={
            "source_summary": (
                "Источник приводит концентрацию 12 ppm, скорость 40 bps и массу 20 kg; "
                "остальной авторский текст написан по-русски."
            )
        },
    ) is None


def test_single_foreign_term_does_not_trigger_a_mechanical_rejection() -> None:
    assert model_authored_language_error(
        output_language="ru",
        fields={
            "source_summary": (
                "Затем booklet объясняет переход к современной системе и завершает исторический обзор."
            )
        },
    ) is None


def test_long_all_latin_official_actor_name_is_not_rejected() -> None:
    assert model_authored_language_error(
        output_language="ru",
        fields={
            "actor": (
                "Hong Kong and Shanghai Banking Corporation; "
                "Chartered Bank of India, Australia and China"
            )
        },
    ) is None


def test_distinct_isolated_foreign_terms_do_not_form_a_fake_phrase() -> None:
    assert model_authored_language_error(
        output_language="ru",
        fields={
            "source_summary": (
                "Термин booklet указан в заголовке, а слово mechanism встречается в отдельном примечании."
            )
        },
    ) is None


def test_embedded_multiword_foreign_technical_phrase_is_rejected() -> None:
    error = model_authored_language_error(
        output_language="ru",
        fields={
            "statement": (
                "Документ связывает реформу с currency board fashion и подробно описывает последующие события."
            )
        },
    )

    assert error is not None
    assert error["invalid_fields"][0]["foreign_lowercase_words"] == [
        "currency",
        "board",
        "fashion",
    ]


def test_latin_script_languages_are_not_guessed_by_a_mechanical_gate() -> None:
    assert model_authored_language_error(
        output_language="fr",
        fields={"statement": "This may be English, but a script-only validator cannot decide safely."},
    ) is None


def test_source_analysis_language_check_ignores_handles_but_catches_authored_fields() -> None:
    error = source_analysis_language_error(
        {
            "source_ref": "S14",
            "source_summary": "Краткое описание источника на русском языке.",
            "findings": [
                {
                    "statement": (
                        "The later reform retained the existing banks, but this sentence is authored entirely "
                        "in English and must be rewritten."
                    ),
                    "support_explanation": "Это объяснение написано по-русски.",
                    "passage_refs": ["P:S14:7"],
                    "scope": {"actor": "Exchange Fund", "period": "1974", "geography": "Hong Kong"},
                    "limitations": ["Не относится к первоначальному возникновению права."],
                }
            ],
        },
        output_language="ru",
    )

    assert error is not None
    assert [item["field"] for item in error["invalid_fields"]] == [
        "findings[0].statement"
    ]


def test_question_resolution_language_check_catches_english_answer() -> None:
    error = question_resolution_language_error(
        {
            "requirement_results": [
                {
                    "established_answer": (
                        "The sources establish a later institutional arrangement but do not establish the "
                        "original causal decision."
                    ),
                    "criterion_match_reason": "Критерий закрыт лишь частично.",
                    "unresolved_remainder": "Не найден первоначальный правовой механизм.",
                }
            ]
        },
        output_language="ru",
    )

    assert error is not None
    assert error["invalid_fields"][0]["field"] == (
        "requirement_results[0].established_answer"
    )
