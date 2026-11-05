import hashlib
import json
import re
from importlib.resources import files

import pytest
from pydantic import ValidationError

from podgen.writing.roles import (
    EditorialFeedback,
    WriteOutput,
    role_instructions,
    role_instructions_for_version,
)


@pytest.mark.parametrize("stage", ["draft", "review", "revise"])
@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_all_role_instructions_use_complete_packaged_resources(stage, format, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = files("podgen.writing.prompts").joinpath("current")
    result = role_instructions(stage, format)
    assert not result.startswith("Instructions: ")
    assert not re.search(r"\b[0-9a-f]{64}\b", result)
    assert not re.search(r"[А-Яа-яЁё]", result)
    assert "output_language" in result
    assert result == "\n\n".join(
        root.joinpath(name).read_bytes().decode("utf-8")
        for name in ("common.md", f"{format}.md", f"{stage}.md")
    )
    for name in ("common.md", f"{format}.md", f"{stage}.md"):
        assert root.joinpath(name).read_bytes().decode("utf-8") in result
    assert result.endswith(root.joinpath(f"{stage}.md").read_bytes().decode("utf-8"))


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
    frozen_schema_hashes = {
        "WriteOutput": "fbdd0f792bf1edf7a39dae39aa43f4c5d64de1cbc7809694eda6f3dd8b284bb5",
        "EditorialFeedback": "9ba37e9851e7f6b142dfc65cbd84ae537f5291e0aa09cbe5fc1193eb0b797edc",
    }
    encoded = json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()
    assert hashlib.sha256(encoded).hexdigest() == frozen_schema_hashes[model.__name__]


@pytest.mark.parametrize(
    "stage,format,expected_sha256",
    [
        ("draft", "solo", "4e18d784d019d454c09ce3e7ec913b3f290e88ac024e8d1ba6a63b16f48a73d4"),
        ("draft", "dialogue", "1e7b4169cadbc77d2cb95ef0cec65ae4e862cd2b96b83c2aec9a50bab01656fd"),
        ("review", "solo", "5ce6a27af12e38467fed6fa616e80969c5b1f41c9923a7d5ab31e1044c2200fa"),
        ("review", "dialogue", "bb60a7fa856cead50e21420fd7c8683bc9aa627059c2adb8273349a12562ed81"),
        ("revise", "solo", "bc55e39c389569a73dd086667641bb460ea65d4f1c14aad72da8b3da948473c7"),
        ("revise", "dialogue", "f4b083fe184994a73116f883b992a2de0f4e52b4bb7b9b756166ea2778bc71db"),
    ],
)
def test_archived_v1_instructions_are_byte_identical(stage, format, expected_sha256):
    text = role_instructions_for_version(stage, format, version="whole-episode-a/1")
    assert hashlib.sha256(text.encode()).hexdigest() == expected_sha256


@pytest.mark.parametrize(
    "stage,format,expected_sha256",
    [
        ("draft", "solo", "58cf539c58a48fffa4ce99fbe7b33b1ef61aaac3fc35c3ed424356df711872a7"),
        ("draft", "dialogue", "f55d7220ab4ca79a444c342d95199daff71d0eb186042b26312a8bfca21aab01"),
        ("review", "solo", "070bf397d1de1cf8cfa03430ce3dd5c40cf0b13abaa26064d19f55d831c2a24b"),
        ("review", "dialogue", "d2b22990b820a7daed1be291beb0dbef6c04e5eef8fa489b77252c3d9e7217f4"),
        ("revise", "solo", "816d8388b61fa29db5cfdec88b156cc55fd30e7d0f257e4c8a754a49d01aa89e"),
        ("revise", "dialogue", "6fde2b0cbbc81d99a7f3f502aad402a5e9cf461780209025cbda52fd0a4f9ffa"),
    ],
)
def test_archived_v2_instructions_are_byte_identical(stage, format, expected_sha256):
    text = role_instructions_for_version(stage, format, version="whole-episode-a/2")
    assert hashlib.sha256(text.encode()).hexdigest() == expected_sha256


@pytest.mark.parametrize("version", ["unpublished", "../v1", None])
def test_unknown_version_never_falls_back_to_current(version):
    with pytest.raises(ValueError, match="unknown_writing_instruction_version"):
        role_instructions_for_version("draft", "solo", version=version)


@pytest.mark.parametrize(
    "stage,format,expected_sha256",
    [
        ("draft", "solo", "2fd6ee403e1ea3b4386fc80c1db210d15d848b65b50adc79f2be782a47106d94"),
        ("draft", "dialogue", "4c46ac69d2f8f789d88ac280ebd40defd3c473fec6a05e0757144f9928872b3a"),
        ("review", "solo", "8778691b8328f55c89760ec131bce6f2b96006cd996a7bd68fe6444d37728348"),
        ("review", "dialogue", "2c67377bfd87d423cacd3be745d28582c98f8e3cc621724146da958a174afa37"),
        ("revise", "solo", "a85e7c071cac4029d903ffa5d46f8e954b4487f2dcfce32c0b184d44a517d043"),
        ("revise", "dialogue", "1d8c3f19b84bcabcf2e58b2e7b670b24f6415891ba23cec2462cfb321265bf22"),
    ],
)
def test_archived_v3_instructions_are_byte_identical(stage, format, expected_sha256):
    text = role_instructions_for_version(stage, format, version="whole-episode-a/3")
    assert hashlib.sha256(text.encode()).hexdigest() == expected_sha256
