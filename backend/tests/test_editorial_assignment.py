"""Optional editorial instructions are absent from the wire unless explicitly supplied."""

import json
from pathlib import Path

import pytest

from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.editorial_schema import packet_schema
from podgen.writing.projections import assignment_view

FIELDS = ("excluded_topics", "style_guidance")


@pytest.mark.parametrize("omit", [False, True])
def test_optional_instructions_need_no_placeholder_and_do_not_enter_wire(omit):
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/editorial-input.v1.example.json").read_text()
    )
    for field in FIELDS:
        if omit:
            data["assignment"].pop(field)
        else:
            data["assignment"][field] = []
    packet = EditorialInput.model_validate(data)
    assert packet.assignment.excluded_topics == []
    assert packet.assignment.style_guidance == []
    assert not set(FIELDS) & assignment_view(packet).keys()
    assert packet.model_dump()["assignment"]["excluded_topics"] == []


@pytest.mark.parametrize("field", FIELDS)
def test_explicit_editorial_instruction_survives_verbatim(field):
    data = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/editorial-input.v1.example.json").read_text()
    )
    for name in FIELDS:
        data["assignment"].pop(name)
    text = "  Не пересказывать вывод в каждой реплике.\nСохранить явно заданное ограничение.  "
    data["assignment"][field] = [text]
    packet = EditorialInput.model_validate(data)
    assert assignment_view(packet)[field] == [text]
    assert packet.model_dump()["assignment"][field] == [text]
    other = next(name for name in FIELDS if name != field)
    assert other not in assignment_view(packet)


def test_published_schema_allows_omission_and_empty_lists():
    schema = packet_schema(version=1)
    assignment = schema["$defs"]["Assignment"]
    assert not set(FIELDS) & set(assignment["required"])
    for field in FIELDS:
        assert assignment["properties"][field]["type"] == "array"
        assert "minItems" not in assignment["properties"][field]
    published = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/editorial-input.v1.schema.json").read_text()
    )
    assert published == schema
