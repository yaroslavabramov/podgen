"""Active Markdown composition has explicit, role-owned dependencies and byte identities."""

import hashlib

import pytest

from podgen.writing import prompts
from podgen.writing.prompts import composition
from tests.test_prompt_resources import deployed_python
from tests.test_prompt_resources import installed_package as installed_package

SNAPSHOT = """
import json
from podgen.writing.prompts import load_instruction_set
print(json.dumps({stage+'/'+fmt:dict(version=p.version,body=p.body)
for fmt in ('solo','dialogue') for stage,p in load_instruction_set(fmt,include_planning=True).items()}))
"""


def test_planner_and_revision_have_no_implicit_format_or_common_dependencies(monkeypatch, tmp_path):
    current = tmp_path / "current"
    current.mkdir()
    (current / "planner.md").write_bytes(b"Choose the story.\r\n")
    (current / "plan-revision.md").write_bytes(b"{{include:planner.md}}\n\nRevise.\n")
    monkeypatch.setattr(composition, "files", lambda _: tmp_path)
    for format in ("solo", "dialogue"):
        assert prompts.role_instructions("plan", format) == "Choose the story.\r\n"
        assert prompts.role_instructions("plan-revise", format) == "Choose the story.\r\n\n\nRevise.\n"


@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_current_ids_hash_actual_bodies_and_planning_format_is_explicit(format):
    assembled = prompts.load_instruction_set(format, include_planning=True)
    other = prompts.load_instruction_set("dialogue" if format == "solo" else "solo", include_planning=True)
    assert assembled["plan"] == other["plan"]
    assert assembled["plan-revise"] == other["plan-revise"]
    assert assembled["plan-review"] != other["plan-review"]
    for prompt in assembled.values():
        assert prompt.version == "whole-episode-a/sha256:" + hashlib.sha256(prompt.body.encode()).hexdigest()
        assert "{{include:" not in prompt.body


@pytest.mark.parametrize(
    "component", ["common", "solo", "dialogue", "planner", "plan-revision", "plan-editor"]
)
def test_installed_planning_content_ids_follow_actual_components(installed_package, component):
    before = deployed_python(installed_package, SNAPSHOT)
    path = installed_package / "podgen/writing/prompts/current" / (component + ".md")
    original = path.read_bytes()
    try:
        path.write_bytes(
            original.replace(b"{{endsection}}", b"\nSYNTHETIC dependency edit.\r\n{{endsection}}")
            if component == "planner"
            else original + b"\nSYNTHETIC dependency edit.\r\n"
        )
        after = deployed_python(installed_package, SNAPSHOT)
        expected = set()
        for key in before:
            stage, format = key.split("/")
            dependencies = (
                {"planner"}
                if stage == "plan"
                else {"planner", "plan-revision"}
                if stage == "plan-revise"
                else {"common", format, composition.ROLES[stage]}
            )
            if component in dependencies:
                expected.add(key)
        assert {key for key in before if before[key] != after[key]} == expected
        assert {key for key in before if before[key]["version"] != after[key]["version"]} == expected
    finally:
        path.write_bytes(original)
