"""Actual fresh manifest/preview composition and immutable saved bodies, without paid calls."""

import hashlib
import json

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.writing.behavior import resolve_behavior
from podgen.writing.prompts import composition, load_role, markdown
from podgen.writing.research_materials.fact_source_instructions import adapt as archived_sources
from podgen.writing.research_materials.runtime_instructions import attach
from tests.source_excerpt_cases import data_and_profile, release
from tests.test_planning_v9 import selected
from tests.test_prompt_resources import installed_package as installed_package


@pytest.mark.parametrize("format", ["solo", "dialogue"])
def test_final_manifest_identity_and_both_previews_follow_exact_current_text(monkeypatch, format):
    from dataclasses import replace

    from podgen.writing.editorial_contract import EditorialInput

    materials, plan, _, _, _, _, _, _, profile = data_and_profile()
    if format == "dialogue":
        raw = materials.packet.model_dump(mode="json")
        raw["assignment"]["format"] = format
        raw["assignment"]["speakers"].append(dict(label="SECOND", function="Ask", speech_register="Plain"))
        materials = replace(materials, packet=EditorialInput.model_validate(raw))
    store, _, command, manifest = release(monkeypatch, materials, profile)
    from podgen.domain.contracts.common import ArtifactRef

    frozen = store.get(
        command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"]["draft"])
    ).payload["template"]
    assert (
        manifest["role_instructions_versions"]["draft"]
        == "whole-episode-a/sha256:" + hashlib.sha256(frozen.encode()).hexdigest()
    )
    arguments = dict(approved_plan=selected(plan))
    if format == "dialogue":
        arguments["approved_plan"]["plan"]["host_functions"].append(
            dict(arguments["approved_plan"]["plan"]["host_functions"][0], speaker_id="SECOND")
        )
    fresh = preview_request(command.run_id, "draft", profile("draft"), materials, **arguments)
    saved = preview_request(
        command.run_id,
        "draft",
        profile("draft"),
        materials,
        snapshot=PreviewSnapshot(manifest, frozen),
        **arguments,
    )
    assert fresh == saved
    read = composition.read

    def changed(path, **kwargs):
        text = read(path, **kwargs)
        return text + "\nSYNTHETIC authored edit.\r\n" if path == "author.md" else text

    monkeypatch.setattr(composition, "read", changed)
    newer = preview_request(command.run_id, "draft", profile("draft"), materials, **arguments)
    assert newer["instructions"] == frozen + "\nSYNTHETIC authored edit.\r\n"
    assert {k: v for k, v in newer.items() if k != "instructions"} == {
        k: v for k, v in fresh.items() if k != "instructions"
    }
    assert (
        preview_request(
            command.run_id,
            "draft",
            profile("draft"),
            materials,
            snapshot=PreviewSnapshot(manifest, frozen),
            **arguments,
        )
        == saved
    )
    assert resolve_behavior().instructions("draft", format).body == newer["instructions"]


def test_historical_source_adapter_reads_no_current_and_rejects_unknown(monkeypatch):
    index = json.loads(composition.read("source-adapters.json", historical=True))
    # A complete archived input is constructed from its saved research role and old operational adapter.
    from podgen.writing.research_materials.instructions import adapt

    base = resolve_behavior("existing/1").instructions("draft", "solo").body
    assert adapt("draft", "solo", base)
    registry = json.loads(composition.read("research-adapters.json", historical=True))
    original = next(
        composition.read(path, historical=True)
        for path in registry.values()
        if path.endswith("fact-review.md") and "/production-1/" in path
    )
    full = attach(original, anchored=True)
    assert hashlib.sha256(full.encode()).hexdigest() in index
    expected = archived_sources(full)
    read = composition.read

    def history_only(path, *, historical=False):
        assert historical, "Historical adaptation reopened current instruction text"
        return read(path, historical=True)

    monkeypatch.setattr(composition, "read", history_only)
    from podgen.writing.research_materials.source_instruction_variant import from_manifest, resolve

    assert resolve(from_manifest({}), full) == expected
    assert archived_sources(full) == expected
    assert archived_sources(expected) == expected
    with pytest.raises(ContractError, match="fact_source_only_instruction_baseline_mismatch"):
        archived_sources(full + "changed")


@pytest.mark.parametrize("sources", ["cards", "research", "sources"])
def test_active_fact_source_variants_are_explicit_and_editable(monkeypatch, sources):
    old = load_role("fact-review", "dialogue", sources=sources)
    read = composition.read
    monkeypatch.setattr(
        composition,
        "read",
        lambda path, **kw: (
            read(path, **kw) + ("\nSYNTHETIC fact edit.\r\n" if path == "fact-review.md" else "")
        ),
    )
    new = load_role("fact-review", "dialogue", sources=sources)
    assert new.body == old.body + "\nSYNTHETIC fact edit.\r\n"
    assert new.version == "fact-review/sha256:" + hashlib.sha256(new.body.encode()).hexdigest()


def test_template_rejects_unknown_conditions_sections_and_cycles():
    for template, error in [
        ("{{if absent}}x{{endif}}", "unknown_writing_prompt_condition"),
        ("{{if sources=cards}}x", "unbalanced_writing_prompt_condition"),
        ("{{include:a.md#missing}}", "unknown_writing_prompt_section"),
        ("{{include:a.md}}", "cyclic_writing_prompt_include"),
        (
            "{{section task}}one{{endsection}}{{section task}}two{{endsection}}",
            "duplicate_writing_prompt_section",
        ),
        ("{{if sources=reseach}}lost{{endif}}", "unknown_writing_prompt_condition_value"),
        ("{{if sources=cards", "malformed_writing_prompt_directive"),
        ("{{section task}}unfinished", "unsupported_writing_prompt_directive"),
        ("{{section unused}}{{if sources=cards{{endsection}}", "malformed_writing_prompt_directive"),
        ("{{if sources=cards}}one{{else}}two{{else}}lost{{endif}}", "unbalanced_writing_prompt_condition"),
    ]:
        with pytest.raises(ContractError, match=error):
            markdown.render("a.md", dict(sources="cards"), lambda _, value=template: value)


def test_wheel_contains_only_named_current_and_separate_history(installed_package):
    root = installed_package / "podgen/writing"
    assert not (root / "behavior/assets").exists()
    current = root / "prompts/current"
    for name in ["author", "fact-verification", "durable-tools", "solo", "dialogue", "style-reference-ru"]:
        assert (current / (name + ".md")).is_file()
    assert (root / "prompts/history/profiles/existing-1.json").is_file()
    assert not list(current.glob("wording/*"))
    assert not any(len(p.stem) == 64 for p in root.rglob("*.md"))


def test_source_variant_and_original_share_one_bounded_file_snapshot(monkeypatch, tmp_path):
    from podgen.writing.prompts.snapshot import instruction_snapshot
    from podgen.writing.research_materials.source_instruction_variant import pin, resolve

    folder = tmp_path / "current"
    folder.mkdir()
    path = folder / "fact-review.md"
    path.write_text("Original task. {{if sources=sources}}Sources.{{else}}Report.{{endif}}")
    monkeypatch.setattr(composition, "files", lambda _: tmp_path)
    behavior = resolve_behavior()
    with instruction_snapshot():
        original = behavior.instructions("fact-review", "solo", sources="research").body
        path.write_text("New current task.")
        variant = pin(behavior, "fact-review", "solo", original)
    assert original == "Original task. Report."
    assert resolve(variant, original) == "Original task. Sources."
    assert behavior.instructions("fact-review", "solo").body == "New current task."
    with pytest.raises(ContractError, match="fact_source_instruction_variant_binding_mismatch"):
        resolve(variant, original + "changed")
    with pytest.raises(ContractError, match="fact_source_instruction_variant_binding_mismatch"):
        resolve(dict(variant, body="incomplete"), original)


def test_runtime_instruction_messages_are_pinned_and_old_manifests_use_frozen_text(monkeypatch, tmp_path):
    from podgen.writing.prompts import research_runtime

    baseline = research_runtime.legacy()
    assert research_runtime.pin()["blocks"] == baseline
    legacy_text = composition.read("runtime/research-continuation-v1.md", historical=True)
    frozen = tmp_path / "history/runtime/research-continuation-v1.md"
    frozen.parent.mkdir(parents=True)
    frozen.write_text(legacy_text)
    current = tmp_path / "current"
    current.mkdir()
    text = "\n".join(
        "{{section " + key + "}}SYNTHETIC " + key + "{{endsection}}" for key in research_runtime.BLOCKS
    )
    (current / "research-continuation.md").write_text(text)
    monkeypatch.setattr(composition, "files", lambda _: tmp_path)
    pinned = research_runtime.pin()
    assert pinned["blocks"] != baseline
    (current / "research-continuation.md").unlink()
    assert research_runtime.from_manifest({research_runtime.KEY: pinned}) == pinned["blocks"]
    with pytest.raises(ContractError, match="invalid_pinned_research_runtime_instructions"):
        research_runtime.from_manifest({research_runtime.KEY: dict(pinned, blocks={})})
    assert research_runtime.from_manifest({}) == baseline
