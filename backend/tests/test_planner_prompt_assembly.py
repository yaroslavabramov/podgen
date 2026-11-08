"""Planner instructions have only planning dependencies, in source and installed wheels."""

import hashlib
import json

import pytest

from podgen.writing import prompts
from tests.test_prompt_resources import deployed_python
from tests.test_prompt_resources import installed_package as installed_package

SNAPSHOT = """
import json
from podgen.writing.prompts import load_instruction_set, role_instructions
result = {}
for format in ('solo','dialogue'):
    for stage,prompt in load_instruction_set(format,include_planning=True).items():
        assert role_instructions(stage,format) == prompt.body
        result[stage+'/'+format] = dict(version=prompt.version,body=prompt.body)
print(json.dumps(result))
"""


def test_planning_roles_read_no_shared_or_format_resource(monkeypatch, tmp_path):
    current = tmp_path / "current"
    current.mkdir()
    plan, revision = "Choose the story.\r\n", "Revise that chosen story.\n"
    (current / "plan.md").write_bytes(plan.encode())
    (current / "plan-revise.md").write_bytes(revision.encode())
    editor = "Assess the whole telling.\n"
    (current / "plan-review.md").write_bytes(editor.encode())
    monkeypatch.setattr(prompts, "files", lambda _: tmp_path)
    for format in ("solo", "dialogue"):
        assert prompts.role_instructions("plan-review", format) == editor
        assert prompts.role_instructions("plan", format) == plan
        assert prompts.role_instructions("plan-revise", format) == plan + "\n\n" + revision


@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_current_assemblies_use_exact_owned_components_and_preserve_other_ids(format):
    root = prompts.files("podgen.writing.prompts").joinpath("current")

    def text(name):
        return root.joinpath(name).read_bytes().decode()

    assembled = prompts.load_instruction_set(format, include_planning=True)
    assert assembled["plan"].body == text("plan.md")
    assert assembled["plan-revise"].body == text("plan.md") + "\n\n" + text("plan-revise.md")
    other_format = "solo" if format == "dialogue" else "dialogue"
    other = prompts.load_instruction_set(other_format, include_planning=True)
    assert assembled["plan"] == other["plan"]
    assert assembled["plan-revise"] == other["plan-revise"]

    assert assembled["plan-review"] == other["plan-review"]
    assert assembled["plan-review"].body == text("plan-review.md")

    for stage in ("draft", "review", "revise"):
        parts = [[name, text(name)] for name in ("common.md", format + ".md", stage + ".md")]
        source = json.dumps(
            ["semantic-body/1", stage, format, parts], ensure_ascii=False, separators=(",", ":")
        ).encode()
        assert assembled[stage].version == "whole-episode-a/sha256:" + hashlib.sha256(source).hexdigest()
        assert assembled[stage].body == "\n\n".join(part[1] for part in parts)


@pytest.mark.parametrize("component", ["common", "solo", "dialogue", "plan", "plan-revise", "plan-review"])
def test_installed_planning_content_ids_follow_actual_components(installed_package, component):
    before = deployed_python(installed_package, SNAPSHOT)
    path = installed_package / "podgen/writing/prompts/current" / (component + ".md")
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\nSYNTHETIC dependency edit.\r\n")
        after = deployed_python(installed_package, SNAPSHOT)
        expected = set()
        for key in before:
            stage, format = key.split("/")
            dependencies = (
                {stage}
                if stage in ("plan", "plan-review")
                else {"plan", "plan-revise"}
                if stage == "plan-revise"
                else {"common", format, stage}
            )
            if component in dependencies:
                expected.add(key)
        assert {key for key in before if before[key] != after[key]} == expected
        assert {key for key in before if before[key]["version"] != after[key]["version"]} == expected
    finally:
        path.write_bytes(original)
