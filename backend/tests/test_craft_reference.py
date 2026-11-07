"""Localized reference bytes are author-only data and preserve old snapshots."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.preview import preview_request
from podgen.writing import craft_reference
from tests.test_editorial_independence import payload
from tests.test_prompt_resources import deployed_python
from tests.test_prompt_resources import installed_package as installed_package
from tests.test_v5_role_wire import fixture


def reference_resource(tmp_path, monkeypatch):
    directory = tmp_path / "examples"
    directory.mkdir()
    resource = directory / "ru.md"
    raw = "Source material\r\nДословный русский образец.\r\n".encode()
    resource.write_bytes(raw)
    monkeypatch.setattr(craft_reference, "files", lambda _: tmp_path)
    return resource, raw


def test_reference_retains_bytes_and_hash_with_language_scoping(tmp_path, monkeypatch):
    resource, raw = reference_resource(tmp_path, monkeypatch)
    reference = craft_reference.load_reference("ru-RU")
    assert reference == dict(language="ru", text=raw.decode(), sha256=sha256(raw).hexdigest())
    manifest = dict(output_language="ru", craft_reference=reference)
    resource.write_text("Другой текст.")
    assert craft_reference.pinned_reference(manifest) == raw.decode()
    assert craft_reference.pinned_reference({}) is None
    for language in ("en", "uk", "zh-CN"):
        assert craft_reference.load_reference(language) is None
    corrupted = deepcopy(manifest)
    corrupted["craft_reference"]["text"] += " Подмена."
    with pytest.raises(ContractError, match="invalid_pinned_craft_reference"):
        craft_reference.pinned_reference(corrupted)
    with pytest.raises(ContractError, match="invalid_pinned_craft_reference"):
        craft_reference.pinned_reference(dict(manifest, output_language="en"))
    resource.unlink()
    with pytest.raises(ContractError, match="craft_reference_unavailable"):
        craft_reference.load_reference("ru")
    assert craft_reference.load_reference("en") is None


def test_fresh_preview_sends_reference_only_to_ru_author_roles(tmp_path, monkeypatch):
    _, raw = reference_resource(tmp_path, monkeypatch)
    materials, _, plan, profile, script, reader, fact, review = fixture()
    values = dict(
        draft=dict(approved_plan=plan),
        revise=dict(approved_plan=plan, draft=script, feedback=dict(reader, reading_mode="informed")),
        plan={},
        **{"plan-review": dict(candidate=plan)},
        review=dict(approved_plan=plan, draft=script),
        **{"reader-review": dict(draft=script), "fact-review": dict(draft=script)},
        **{"revise-1": dict(approved_plan=plan, draft=script, reader_feedback=reader, fact_feedback=fact)},
    )
    for stage, inputs in values.items():
        body = preview_request(uuid4(), stage, profile, materials, **inputs)
        visible = payload(body)
        if stage in ("draft", "revise", "revise-1"):
            assert visible["craft_reference"] == raw.decode()
            assert "Source material" not in body["instructions"]
        else:
            assert "craft_reference" not in visible
    english = replace(
        materials,
        packet=materials.packet.model_copy(
            update=dict(assignment=materials.packet.assignment.model_copy(update=dict(output_language="en")))
        ),
    )
    assert "craft_reference" not in payload(
        preview_request(uuid4(), "draft", profile, english, approved_plan=plan)
    )


def test_missing_author_reference_does_not_block_other_role_previews(tmp_path, monkeypatch):
    resource, _ = reference_resource(tmp_path, monkeypatch)
    resource.unlink()
    materials, _, plan, profile, script, reader, _, review = fixture()
    for stage, inputs in {
        "plan": {},
        "plan-review": dict(candidate=plan),
        "plan-revise": dict(candidate=plan, plan_review=review),
        "reader-review": dict(draft=script),
        "fact-review": dict(draft=script),
        "review": dict(approved_plan=plan, draft=script),
    }.items():
        assert "craft_reference" not in payload(preview_request(uuid4(), stage, profile, materials, **inputs))
    with pytest.raises(ContractError, match="craft_reference_unavailable"):
        preview_request(uuid4(), "draft", profile, materials, approved_plan=plan)


def test_installed_wheel_contains_and_reads_the_exact_russian_reference(installed_package):
    result = deployed_python(
        installed_package,
        """
import hashlib, json
from importlib.resources import files
from podgen.writing.craft_reference import load_reference
raw = files('podgen.writing.prompts').joinpath('examples', 'ru.md').read_bytes()
reference = load_reference('ru-RU')
assert reference['text'].encode() == raw
assert reference['sha256'] == hashlib.sha256(raw).hexdigest()
assert load_reference('en') is None
print(json.dumps(dict(sha256=reference['sha256'], bytes=len(raw))))
""",
    )
    from importlib.resources import files

    raw = files("podgen.writing.prompts").joinpath("examples", "ru.md").read_bytes()
    assert result == dict(sha256=sha256(raw).hexdigest(), bytes=len(raw))
