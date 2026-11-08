"""Real primary-source transfer packet: published schema, exact ranges and ordinary source admission."""

import hashlib
import json
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
from podgen.writing.editorial_contract import EditorialInput
from tests.test_editorial_independence import invoke

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "evals/reports/2026-11-08-story-editing/transfer/input"
FROZEN_SHA256 = "f92ace1fc2ed82b2bea34565f5ada718792a7406862caf1fae8cd04989a850d8"


def test_complete_wright_packet_and_all_sources_match_the_published_contract():
    raw = (INPUT / "shared.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_SHA256
    data = json.loads(raw)
    schema = json.loads((ROOT / "backend/schemas/editorial-input.v2.schema.json").read_text())
    Draft202012Validator(schema).validate(data)
    packet = EditorialInput.model_validate(data)
    assert packet.model_dump(mode="json") == data
    texts = {}
    for document in packet.documents:
        path = PurePosixPath(document.source_path)
        assert not path.is_absolute() and ".." not in path.parts
        content = (INPUT / document.source_path).read_bytes()
        assert hashlib.sha256(content).hexdigest() == document.sha256
        texts[document.key] = content.decode()
    for card in packet.dossier.knowledge_cards:
        for excerpt in card.excerpts:
            assert texts[excerpt.document][excerpt.start : excerpt.end] == excerpt.text


def test_wright_sources_and_shared_packet_can_be_admitted_without_a_provider(
    repo, scope, tmp_path, monkeypatch
):
    from podgen import writing_cli

    project, _ = scope
    monkeypatch.setenv("PODGEN_DATABASE_URL", repo.database.engine.url.render_as_string(hide_password=False))

    def no_provider(*args, **kwargs):
        raise AssertionError("source import must not construct a provider")

    monkeypatch.setattr(writing_cli, "OpenAIProvider", no_provider)
    prefix = ["--project", str(project), "--actor", str(repo.actor_id), "--objects", str(repo.objects.root)]
    data = json.loads((INPUT / "shared.json").read_text())
    mapping = {}
    for document in data["documents"]:
        output = tmp_path / (document["key"] + "-ref.json")
        assert (
            invoke(
                prefix,
                "editorial-source-import",
                file=INPUT / document["source_path"],
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
        invoke(prefix, "editorial-import", input=INPUT / "shared.json", source_map=source_map, out=shared_ref)
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
        assert document.text == (INPUT / spec.source_path).read_text()
