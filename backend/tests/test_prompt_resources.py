"""One editable component changes only its dependants, including in an installed wheel."""

import hashlib
import inspect
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from podgen.domain.contracts.common import ArtifactRef
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.writing import prompts
from tests.test_writing_evaluation import FakeProvider, run
from tests.test_writing_evaluation import setup_writer as setup_writer


@pytest.fixture(scope="module")
def installed_package(tmp_path_factory):
    directory = tmp_path_factory.mktemp("installed-podgen")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(directory / "dist"), str(root)],
        check=True,
        capture_output=True,
    )
    wheel = next((directory / "dist").glob("*.whl"))
    target = directory / "site-packages"
    subprocess.run(
        ["uv", "pip", "install", "--offline", "--no-deps", "--target", str(target), str(wheel)],
        check=True,
        capture_output=True,
    )
    return target


def deployed_python(target, script, *args):
    # Isolated import path, outside the checkout; a missing wheel resource cannot fall back to source.
    prefix = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv.pop(1)); "
    prefix += "import podgen; assert Path(podgen.__file__).is_relative_to(sys.path[0]);\n"
    completed = subprocess.run(
        [sys.executable, "-I", "-c", prefix + script, str(target), *map(str, args)],
        cwd=target.parent,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


SNAPSHOT = """
import hashlib, json
from podgen.writing.prompts import load_anchor_repair_instructions, load_instruction_set, role_instructions, role_instructions_for_version
active = {}
history = {}
for format in ("solo", "dialogue"):
    for stage, prompt in load_instruction_set(format).items():
        assert role_instructions(stage, format) == prompt.body
        active[stage + "/" + format] = dict(version=prompt.version, body=prompt.body)
        for version in ("whole-episode-a/1", "whole-episode-a/2", "whole-episode-a/3"):
            body = role_instructions_for_version(stage, format, version=version)
            history[stage + "/" + format + "/" + version] = hashlib.sha256(body.encode()).hexdigest()
repair = load_anchor_repair_instructions()
print(json.dumps(dict(active=active, history=history, repair=dict(version=repair.version, body=repair.body))))
"""


@pytest.mark.parametrize(
    "component", ["common", "solo", "dialogue", "draft", "review", "revise", "anchor-repair"]
)
def test_edit_one_installed_component_changes_only_applicable_bodies(installed_package, component):
    target = installed_package
    before = deployed_python(target, SNAPSHOT)
    path = target / "podgen/writing/prompts/current" / f"{component}.md"
    original = path.read_bytes()
    try:
        # A real single-file edit, including exact CRLF bytes; no generated files or registration.
        path.write_bytes(original + b"\nSYNTHETIC component edit.\r\n")
        after = deployed_python(target, SNAPSHOT)
        assert after["history"] == before["history"]
        assert (after["repair"] != before["repair"]) == (component == "anchor-repair")
        for key, old in before["active"].items():
            stage, format = key.split("/")
            affected = component in ("common", stage, format)
            assert (after["active"][key]["version"] != old["version"]) == affected
            assert (after["active"][key]["body"] != old["body"]) == affected
            if affected:
                assert "SYNTHETIC component edit.\r\n" in after["active"][key]["body"]
        with pytest.raises(subprocess.CalledProcessError):
            deployed_python(
                target,
                f"""from podgen.writing.prompts import role_instructions_for_version
role_instructions_for_version("review", "solo", version={before["active"]["review/solo"]["version"]!r})""",
            )
    finally:
        path.write_bytes(original)
    assert deployed_python(target, SNAPSHOT) == before


@pytest.mark.parametrize("include_planning", [False, True])
def test_instruction_set_reads_each_used_component_once(monkeypatch, include_planning):
    reads = []
    root = prompts.files("podgen.writing.prompts")

    class Resource:
        def __init__(self, path):
            self.path = path

        def joinpath(self, name):
            return Resource(self.path.joinpath(name))

        def read_bytes(self):
            reads.append(self.path.name)
            return self.path.read_bytes()

    monkeypatch.setattr(prompts, "files", lambda _: Resource(root))
    assembly = prompts.load_instruction_set("solo", include_planning=include_planning)
    expected = ["common.md", "solo.md", "draft.md", "review.md", "revise.md"]
    stages = {"draft", "review", "revise"}
    if include_planning:
        expected += ["plan.md", "plan-review.md", "plan-revise.md"]
        stages |= {"plan", "plan-review", "plan-revise"}
    assert reads == expected
    assert set(assembly) == stages


def test_content_id_is_exact_ordered_and_has_no_generated_header(monkeypatch, tmp_path):
    current = tmp_path / "current"
    current.mkdir()
    components = [("common.md", b"same\r\n"), ("solo.md", b"format"), ("review.md", b"stage")]
    for name, content in components:
        (current / name).write_bytes(content)
    monkeypatch.setattr(prompts, "files", lambda _: tmp_path)
    source = json.dumps(
        ["semantic-body/1", "review", "solo", [[name, data.decode()] for name, data in components]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    version = "whole-episode-a/sha256:" + hashlib.sha256(source).hexdigest()
    body = prompts.role_instructions("review", "solo")
    assert body == "same\r\n\n\nformat\n\nstage"
    assert (
        prompts._assemble("review", "solo", {name: data.decode() for name, data in components}).version
        == version
    )
    (current / "common.md").write_bytes(b"same\n")
    assert prompts.role_instructions("review", "solo") != body
    # Reassigning the same concatenated bytes across component boundaries is also a different assembly.
    (current / "common.md").write_bytes(b"same\r\n\n\nformat")
    (current / "solo.md").write_bytes(b"")
    assert (
        prompts._assemble("review", "solo", prompts._read(current, [name for name, _ in components])).version
        != version
    )


@pytest.mark.parametrize(
    "historical_version", [None, "whole-episode-a/1", "whole-episode-a/2", "whole-episode-a/3"]
)
def test_installed_resume_and_replay_use_archived_bodies_without_prompt_files(
    repo, setup_writer, installed_package, tmp_path, monkeypatch, historical_version
):
    command, loader, _ = setup_writer
    if historical_version:
        from podgen.writing.behavior import BehaviorProfile

        released = BehaviorProfile.instructions

        def recorded_role(profile, stage, format):
            if stage not in ("draft", "review", "revise"):
                return released(profile, stage, format)
            return prompts.RoleInstructions(
                historical_version,
                prompts.role_instructions_for_version(stage, format, version=historical_version),
            )

        monkeypatch.setattr(BehaviorProfile, "instructions", recorded_role)
        create_run = EvaluationJournal.create_run

        def legacy_manifest(journal, command, manifest):
            # Reproduce the old manifest shape before its first immutable persistence.
            legacy = dict(manifest, role_instructions_version=historical_version)
            legacy.pop("anchor_repair_instructions", None)
            return create_run(journal, command, legacy)

        monkeypatch.setattr(EvaluationJournal, "create_run", legacy_manifest)
    provider = FakeProvider()

    def interrupt_before_review_count():
        if len(provider.requests) == 1:
            raise KeyboardInterrupt()

    provider.on_count = interrupt_before_review_count
    with pytest.raises(KeyboardInterrupt):
        run(repo, command, loader, provider)
    journal = EvaluationJournal(repo, command.project_id)
    manifest = journal.manifest(command.run_id)
    assert journal.invocation(command.run_id, "revise") is None
    target = tmp_path / "deployed"
    shutil.copytree(installed_package, target)
    # Neither active nor numbered source packages remain on this deployed host.
    resource_root = target / "podgen/writing/prompts"
    for name in ("current", "v1", "v2", "v3"):
        shutil.rmtree(resource_root / name)
    config = tmp_path / "resume.json"
    config.write_text(
        json.dumps(
            dict(
                url=repo.database.engine.url.render_as_string(hide_password=False),
                objects=str(repo.objects.root),
                command=command.model_dump(mode="json"),
            )
        )
    )
    # Ship only the offline provider/loader fixtures to the isolated interpreter, never the checkout.
    script = """
import json
from pathlib import Path
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.contracts import WritingRunCommand
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import WireReply, ProviderFault
from podgen.writing.materials import AuthorDocument, AuthorMaterials
config = json.loads(Path(sys.argv[1]).read_text())
command = WritingRunCommand.model_validate(config["command"])
repo = Repository(Database(config["url"]), FileObjectStore(Path(config["objects"])), command.actor_id, purpose="evaluation")
"""
    script += inspect.getsource(FakeProvider) + "\n" + textwrap.dedent(inspect.getsource(loader))
    script += """
provider = FakeProvider()
app = WritingEvaluation(repo, lambda profile: provider, materials_loader=loader, legacy_offline_transport=provider)
result = app.run(command)
assert app.run(command) == result
assert len(provider.requests) == 2
print(json.dumps(dict(result=result, requests=provider.requests)))
"""
    result = deployed_python(target, script, config)
    assert result["result"]["status"] == "completed_unaccepted"
    assert journal.manifest(command.run_id) == manifest
    for request in result["requests"]:
        stage = request["metadata"]["podgen_stage"]
        archived = repo.get(command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"][stage]))
        assert request["instructions"] == archived.payload["template"]
        if historical_version:
            assert (
                request["instructions"].split(";", 1)[0].removeprefix("Instructions: ") == historical_version
            )
        else:
            assert not request["instructions"].startswith("Instructions: ")
            assert manifest["role_instructions_versions"][stage].startswith("whole-episode-a/sha256:")


@pytest.mark.parametrize("version", ["whole-episode-a/1", "whole-episode-a/2", "whole-episode-a/3"])
def test_numbered_history_preserves_original_universal_newline_semantics(version, monkeypatch, tmp_path):
    before = {
        (stage, format): prompts.role_instructions_for_version(stage, format, version=version)
        for stage in ("draft", "review", "revise")
        for format in ("solo", "dialogue")
    }
    directory = "v" + version.rsplit("/", 1)[1]
    shutil.copytree(prompts.files("podgen.writing.prompts").joinpath(directory), tmp_path / directory)
    common = tmp_path / directory / "common.md"
    common.write_bytes(common.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(prompts, "files", lambda _: tmp_path)
    for (stage, format), body in before.items():
        assert prompts.role_instructions_for_version(stage, format, version=version) == body


@pytest.mark.parametrize("stage", ["plan", "plan-review", "plan-revise"])
@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_planning_current_public_api_matches_run_assembly(stage, format):
    assembled = prompts.load_instruction_set(format, include_planning=True)
    assert prompts.role_instructions(stage, format) == assembled[stage].body
    with pytest.raises(ValueError, match="unknown_writing_stage"):
        prompts.role_instructions_for_version(stage, format, version="whole-episode-a/3")


def test_edit_installed_plan_review_only_changes_plan_review_in_both_formats(installed_package):
    script = """
import json
from podgen.writing.prompts import load_instruction_set
print(json.dumps({stage+'/'+format:dict(version=p.version,body=p.body)
for format in ('solo','dialogue') for stage,p in load_instruction_set(format,include_planning=True).items()}))
"""
    before = deployed_python(installed_package, script)
    path = installed_package / "podgen/writing/prompts/current/plan-review.md"
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\nSYNTHETIC revised plan editor guidance.\n")
        after = deployed_python(installed_package, script)
        assert {key for key in before if before[key] != after[key]} == {
            "plan-review/solo",
            "plan-review/dialogue",
        }
    finally:
        path.write_bytes(original)
