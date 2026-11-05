import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from podgen.writing.roles import (
    ROLE_INSTRUCTIONS_VERSION,
    EditorialFeedback,
    WriteOutput,
    role_instructions,
)


@pytest.mark.parametrize("stage", ["draft", "review", "revise"])
@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_all_role_instructions_use_complete_packaged_resources(stage, format, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = files("podgen.writing.prompts").joinpath("v1")
    result = role_instructions(stage, format)
    assert ROLE_INSTRUCTIONS_VERSION in result
    assert f"stage={stage}; format={format}" in result
    for name in ("common.md", f"{format}.md", f"{stage}.md"):
        assert root.joinpath(name).read_text(encoding="utf-8") in result
    assert result.endswith(root.joinpath(f"{stage}.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize("stage,format", [("other", "solo"), ("draft", "other"), (None, "solo")])
def test_unknown_instruction_selection_fails_explicitly(stage, format):
    with pytest.raises(ValueError, match="unknown_writing_"):
        role_instructions(stage, format)


def test_full_spoken_text_survives_json_roundtrip_without_trimming_or_length_caps():
    text = "  Начало.\n" + "Связный текст ё 😀 е\u0301.\n" * 20_000 + "Последний абзац.  "
    payload = {"utterances": [{"speaker": "host_a", "text": text}]}
    output = WriteOutput.model_validate(payload)
    restored = WriteOutput.model_validate_json(output.model_dump_json())
    assert restored.utterances[0].text == text
    assert restored.model_dump() == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"utterances": []},
        {"utterances": [{"speaker": " ", "text": "Текст."}]},
        {"utterances": [{"speaker": "host_a", "text": "\n\t "}]},
        {"utterances": [{"speaker": "host_a", "text": 42}]},
        {"utterances": [{"speaker": "host_a", "text": "Текст.", "note": "Лишнее поле"}]},
        {"utterances": [{"speaker": "host_a", "text": "Текст."}], "summary": "Пересказ"},
    ],
)
def test_write_output_rejects_structural_errors(payload):
    with pytest.raises(ValidationError):
        WriteOutput.model_validate(payload)


@pytest.fixture
def feedback_payload():
    anchor = {"utterance_index": 0, "quote": "Первая мысль."}
    return {
        "reading_mode": "informed",
        "listener_answer": "Понятен вопрос, но переход к ответу не объяснён.",
        "important_limits": [],
        "strengths": [{"anchors": [anchor], "reason": "Ясное начало."}],
        "findings": [
            {
                "dimension": "transition",
                "priority": "important",
                "anchors": [anchor],
                "observation": "Следующая мысль не следует из названного действия.",
                "requested_change": "Объяснить переход перед выводом.",
                "preserve": ["Сохранить начало."],
            }
        ],
        "research_requests": [
            {
                "anchors": [anchor],
                "question": "Как обоснован переход?",
                "why_needed": "Без него нет ответа.",
            }
        ],
        "duration_note": "Аудио не представлено; длительность не измерена.",
        "recommendation": "needs_material",
    }


def test_actionable_feedback_binds_to_exact_input_and_roundtrips(feedback_payload):
    script = WriteOutput(utterances=[{"speaker": "host_a", "text": "Первая мысль.\nВторая мысль."}])
    feedback = EditorialFeedback.model_validate(feedback_payload)
    feedback.validate_against(script)
    assert EditorialFeedback.model_validate_json(feedback.model_dump_json()) == feedback


@pytest.mark.parametrize("collection", ["strengths", "findings", "research_requests"])
@pytest.mark.parametrize("mutation", ["outside", "wrong_quote"])
def test_feedback_anchor_checks_cover_all_feedback_collections(feedback_payload, collection, mutation):
    anchor = (
        {"utterance_index": 1, "quote": "Первая мысль."}
        if mutation == "outside"
        else {
            "utterance_index": 0,
            "quote": "Несуществующая цитата.",
        }
    )
    feedback_payload[collection][0]["anchors"] = [anchor]
    feedback = EditorialFeedback.model_validate(feedback_payload)
    script = WriteOutput(utterances=[{"speaker": "host_a", "text": "Первая мысль."}])
    with pytest.raises(ValueError, match="feedback_"):
        feedback.validate_against(script)


@pytest.mark.parametrize("index", [-1, True, "0"])
def test_feedback_indices_are_strict_nonnegative_integers(feedback_payload, index):
    feedback_payload["findings"][0]["anchors"][0]["utterance_index"] = index
    with pytest.raises(ValidationError):
        EditorialFeedback.model_validate(feedback_payload)


def test_keep_does_not_force_findings_or_a_rewrite(feedback_payload):
    feedback_payload.update(findings=[], research_requests=[], recommendation="keep")
    feedback = EditorialFeedback.model_validate(feedback_payload)
    assert not feedback.findings and feedback.recommendation == "keep"


def test_feedback_cannot_hide_actionable_content_in_unknown_fields(feedback_payload):
    feedback_payload["findings"][0]["preview"] = "Другое замечание."
    with pytest.raises(ValidationError):
        EditorialFeedback.model_validate(feedback_payload)


@pytest.mark.parametrize("model", [WriteOutput, EditorialFeedback])
def test_json_schema_is_closed_and_contains_no_text_length_limit(model):
    schema = model.model_json_schema()
    definitions = [schema, *schema.get("$defs", {}).values()]
    for definition in definitions:
        if definition.get("type") == "object":
            assert definition["additionalProperties"] is False
            assert set(definition["required"]) == set(definition["properties"])
    assert "maxLength" not in json.dumps(schema)
