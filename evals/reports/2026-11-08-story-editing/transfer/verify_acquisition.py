"""Optional integrity checks for this recorded experiment. Run with pytest on this file."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

INPUT = Path(__file__).resolve().parent / "input"
CORRECTED = INPUT.parent / "input-corrected"
ROOT = INPUT.parents[4]


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
    next(document for document in expected["documents"] if document["key"] == "D1")[
        "sha256"
    ] = hashlib.sha256((CORRECTED / "dossier.md").read_bytes()).hexdigest()
    assert corrected == expected
    assert (CORRECTED / "dossier.md").read_text() == (
        INPUT / "dossier.md"
    ).read_text().replace(
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
    assert (
        hashlib.sha256(source_pdf.read_bytes()).hexdigest()
        == provenance["reproduction"]["sha256"]
    )


def test_corrected_generator_reproduces_the_package_and_refuses_to_overwrite_the_experiment(
    tmp_path,
):
    spec = importlib.util.spec_from_file_location(
        "prepare_wright_transfer", INPUT.parent / "prepare_case.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.main(tmp_path)
    assert (tmp_path / "shared.json").read_bytes() == (
        CORRECTED / "shared.json"
    ).read_bytes()
    assert (tmp_path / "dossier.md").read_bytes() == (
        CORRECTED / "dossier.md"
    ).read_bytes()
    assert not list(tmp_path.rglob("*.pdf"))
    with pytest.raises(ValueError, match="cannot_overwrite_frozen_wright_experiment"):
        builder.main(INPUT)
