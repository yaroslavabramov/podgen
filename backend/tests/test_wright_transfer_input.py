"""Real primary-source transfer packet: published schema, exact ranges and ordinary source admission."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest
from jsonschema import Draft202012Validator

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.writing.editorial_contract import EditorialInput
from tests.test_editorial_independence import invoke

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "evals/reports/2026-11-08-story-editing/transfer/input"
CORRECTED = INPUT.parent / "input-corrected"
FROZEN_SHA256 = "f92ace1fc2ed82b2bea34565f5ada718792a7406862caf1fae8cd04989a850d8"
CORRECTED_SHA256 = "b2a35b11118e60576a2873e935fcb65b847ea86c3ba47e60fd21dd371773fd80"
VERSIONS = [(INPUT, FROZEN_SHA256), (CORRECTED, CORRECTED_SHA256)]


@pytest.mark.parametrize(("input_dir", "expected_sha256"), VERSIONS, ids=["experiment", "corrected"])
def test_complete_wright_packet_and_all_sources_match_the_published_contract(input_dir, expected_sha256):
    raw = (input_dir / "shared.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected_sha256
    data = json.loads(raw)
    schema = json.loads((ROOT / "backend/schemas/editorial-input.v2.schema.json").read_text())
    Draft202012Validator(schema).validate(data)
    packet = EditorialInput.model_validate(data)
    assert packet.model_dump(mode="json") == data
    texts = {}
    for document in packet.documents:
        path = PurePosixPath(document.source_path)
        assert not path.is_absolute() and ".." not in path.parts
        content = (input_dir / document.source_path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == document.sha256
        texts[document.key] = content.decode()
    for card in packet.dossier.knowledge_cards:
        for excerpt in card.excerpts:
            assert texts[excerpt.document][excerpt.start : excerpt.end] == excerpt.text


@pytest.mark.parametrize(("input_dir", "expected_sha256"), VERSIONS, ids=["experiment", "corrected"])
def test_wright_sources_and_shared_packet_can_be_admitted_without_a_provider(
    repo, scope, tmp_path, monkeypatch, input_dir, expected_sha256
):
    from podgen import writing_cli

    project, _ = scope
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))

    def no_provider(*args, **kwargs):
        raise AssertionError("source import must not construct a provider")

    monkeypatch.setattr(writing_cli, "OpenAIProvider", no_provider)
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    raw = (input_dir / "shared.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected_sha256
    data = json.loads(raw)
    mapping = {}
    for document in data["documents"]:
        output = tmp_path / (document["key"] + "-ref.json")
        assert (
            invoke(
                prefix,
                "editorial-source-import",
                file=input_dir / document["source_path"],
                source_path=document["source_path"],
                purpose=document["purpose"],
                scope="wright-transfer-test",
                out=output,
            )
            == 0
        )
        mapping[document["source_path"]] = json.loads(output.read_text())
    source_map = tmp_path / "source-map.json"
    source_map.write_text(json.dumps(mapping))
    shared_ref = tmp_path / "shared-ref.json"
    assert (
        invoke(
            prefix, "editorial-import", input=input_dir / "shared.json", source_map=source_map, out=shared_ref
        )
        == 0
    )
    materials = load_editorial_materials(
        generation_repo(repo),
        project,
        RuntimeRef.model_validate_json(shared_ref.read_bytes()),
        case_id="wright-first-flight",
    )
    assert materials.packet.model_dump(mode="json") == data
    assert materials.fixed is None
    for document, spec in zip(materials.documents, materials.packet.documents, strict=True):
        assert document.text == (input_dir / spec.source_path).read_text()


def test_distance_correction_preserves_fact_identity_primary_bytes_and_the_entire_original_tree():
    original = json.loads((INPUT / "shared.json").read_text())
    corrected = json.loads((CORRECTED / "shared.json").read_text())
    revision = json.loads((CORRECTED / "correction.json").read_text())
    expected = deepcopy(original)
    card = next(
        card
        for card in expected["dossier"]["knowledge_cards"]
        if card["card_id"] == revision["changed_card_id"]
    )
    for field in ("claim", "story_facts"):
        card[field] = card[field].replace(
            "Орвилл записал 852 фута над землёй за 59 секунд",
            "Орвилл записал пройденное за 59 секунд расстояние относительно земли — 852 фута",
        )
    next(document for document in expected["documents"] if document["key"] == "D1")["sha256"] = (
        hashlib.sha256((CORRECTED / "dossier.md").read_bytes()).hexdigest()
    )
    assert corrected == expected
    assert (CORRECTED / "dossier.md").read_text() == (INPUT / "dossier.md").read_text().replace(
        "Орвилл записал 852 фута над землёй за 59 секунд",
        "Орвилл записал пройденное за 59 секунд расстояние относительно земли — 852 фута",
    )
    for document in original["documents"]:
        if document["purpose"] != "dossier":
            assert (INPUT / document["source_path"]).read_bytes() == (
                CORRECTED / document["source_path"]
            ).read_bytes()
    actual = {
        str(path.relative_to(INPUT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in INPUT.rglob("*")
        if path.is_file()
    }
    assert actual == revision["original_tree_sha256"]
    assert not list(CORRECTED.rglob("*.pdf"))
    provenance = json.loads((CORRECTED / "provenance.json").read_text())
    source_pdf = ROOT / provenance["reproduction"]["repository_path"]
    assert hashlib.sha256(source_pdf.read_bytes()).hexdigest() == provenance["reproduction"]["sha256"]


def test_corrected_generator_reproduces_the_package_and_refuses_to_overwrite_the_experiment(tmp_path):
    spec = importlib.util.spec_from_file_location("prepare_wright_transfer", INPUT.parent / "prepare_case.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.main(tmp_path)
    assert (tmp_path / "shared.json").read_bytes() == (CORRECTED / "shared.json").read_bytes()
    assert (tmp_path / "dossier.md").read_bytes() == (CORRECTED / "dossier.md").read_bytes()
    assert not list(tmp_path.rglob("*.pdf"))
    with pytest.raises(ValueError, match="cannot_overwrite_frozen_wright_experiment"):
        builder.main(INPUT)
