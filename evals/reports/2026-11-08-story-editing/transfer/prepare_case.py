"""Reproduce the distance correction from the immutable Wright experiment, without copying its PDF."""

import argparse
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from podgen.writing.editorial_contract import EditorialInput

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
ORIGINAL_INPUT = HERE / "input"
CORRECTED_INPUT = HERE / "input-corrected"
ORIGINAL_SHA256 = "f92ace1fc2ed82b2bea34565f5ada718792a7406862caf1fae8cd04989a850d8"
CORRECTED_SHA256 = "b2a35b11118e60576a2873e935fcb65b847ea86c3ba47e60fd21dd371773fd80"
BEFORE = "Орвилл записал 852 фута над землёй за 59 секунд"
AFTER = (
    "Орвилл записал пройденное за 59 секунд расстояние относительно земли — 852 фута"
)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def replace_once(text):
    if text.count(BEFORE) != 1:
        raise ValueError("unexpected_wright_distance_wording")
    return text.replace(BEFORE, AFTER)


def main(output_dir=CORRECTED_INPUT):
    output_dir = Path(output_dir).resolve()
    original = ORIGINAL_INPUT.resolve()
    if output_dir == original or original in output_dir.parents:
        raise ValueError("cannot_overwrite_frozen_wright_experiment")
    raw = (ORIGINAL_INPUT / "shared.json").read_bytes()
    assert sha256(raw) == ORIGINAL_SHA256
    packet = deepcopy(json.loads(raw))
    card = next(
        card
        for card in packet["dossier"]["knowledge_cards"]
        if card["label"] == "Четвёртый полёт"
    )
    for field in ("claim", "story_facts"):
        card[field] = replace_once(card[field])

    output_dir.mkdir(parents=True, exist_ok=True)
    texts = {}
    for document in packet["documents"]:
        source = ORIGINAL_INPUT / document["source_path"]
        content = source.read_bytes()
        assert sha256(content) == document["sha256"]
        if document["key"] == "D1":
            content = replace_once(content.decode("utf-8")).encode("utf-8")
            document["sha256"] = sha256(content)
        destination = output_dir / document["source_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        texts[document["key"]] = content.decode("utf-8")

    schema_path = ROOT / "backend/schemas/editorial-input.v2.schema.json"
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(packet)
    EditorialInput.model_validate(packet)
    quote_count = 0
    for fact in packet["dossier"]["knowledge_cards"]:
        for excerpt in fact["excerpts"]:
            assert (
                texts[excerpt["document"]][excerpt["start"] : excerpt["end"]]
                == excerpt["text"]
            )
            quote_count += 1
    save(output_dir / "shared.json", packet)
    assert sha256((output_dir / "shared.json").read_bytes()) == CORRECTED_SHA256

    provenance = json.loads((ORIGINAL_INPUT / "provenance.json").read_text())
    download = provenance["reproduction"].pop("download")
    source_pdf = ORIGINAL_INPUT / download
    assert sha256(source_pdf.read_bytes()) == provenance["reproduction"]["sha256"]
    provenance["reproduction"]["repository_path"] = str(source_pdf.relative_to(ROOT))
    provenance["revision"] = {
        "original_shared_sha256": ORIGINAL_SHA256,
        "correction": "852 feet is the distance travelled over the ground in 59 seconds, not altitude.",
        "primary_documents_changed": False,
        "card_identities_changed": False,
    }
    save(output_dir / "provenance.json", provenance)
    save(
        output_dir / "correction.json",
        {
            "original_shared_sha256": ORIGINAL_SHA256,
            "corrected_shared_sha256": CORRECTED_SHA256,
            "changed_card_id": card["card_id"],
            "changed_fields": ["claim", "story_facts"],
            "changed_source_document": "D1",
            "primary_documents": {
                document["key"]: document["sha256"]
                for document in packet["documents"]
                if document["purpose"] == "primary"
            },
            "original_tree_sha256": {
                str(path.relative_to(ORIGINAL_INPUT)): sha256(path.read_bytes())
                for path in sorted(ORIGINAL_INPUT.rglob("*"))
                if path.is_file()
            },
        },
    )
    save(
        output_dir / "validation.json",
        {
            "shared_sha256": CORRECTED_SHA256,
            "original_shared_sha256": ORIGINAL_SHA256,
            "schema": "backend/schemas/editorial-input.v2.schema.json",
            "schema_sha256": sha256(schema_path.read_bytes()),
            "external_json_schema": "passed (Draft202012Validator)",
            "pydantic": "passed",
            "exact_excerpt_ranges": quote_count,
            "knowledge_cards": len(packet["dossier"]["knowledge_cards"]),
            "primary_documents": sum(
                document["purpose"] == "primary" for document in packet["documents"]
            ),
            "file_sha256": {
                document["source_path"]: document["sha256"]
                for document in packet["documents"]
            },
            "model_calls": 0,
        },
    )
    print(CORRECTED_SHA256)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=CORRECTED_INPUT)
    main(parser.parse_args().out_dir)
