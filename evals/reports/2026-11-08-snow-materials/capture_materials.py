"""Record canonical and model-visible material inputs; no provider construction."""

import json
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.model_references import ModelReferences
from podgen.writing.projection_versions import V9
from podgen.writing.projections import role_payload

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CASE = ROOT / "evals/editorial-inputs/current/snow-pump"
FINAL_SHA256 = "b52d0f03e8bc24fcee4c10960011557d8fe7e9a3adc74a1f36f721d3a0b080f5"


def save(name, value):
    (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    raw = (CASE / "history/before-percolation-2026-11-08.json").read_bytes()
    assert sha256(raw).hexdigest() == FINAL_SHA256
    packet = EditorialInput.model_validate_json(raw)
    materials = EditorialMaterials(
        packet, RuntimeRef(record_kind="user_input", record_id=UUID(int=1)), ()
    )
    canonical = role_payload("plan", materials, projection_version=V9)
    visible = ModelReferences.create(packet).translate(canonical)
    save("planner-materials-v9.json", canonical)
    save("planner-visible-v9.json", visible)
    save(
        "frozen-materials.json",
        {
            "shared_sha256": FINAL_SHA256,
            "shared_digest": digest(packet.model_dump(mode="json")),
            "planner_visible_digest": digest(visible),
            "narrative_passages": sum(
                len(card.get("source_excerpts", []))
                for card in visible["dossier"]["knowledge_cards"]
            ),
            "full_fact_excerpts": sum(
                len(card.excerpts) for card in packet.dossier.knowledge_cards
            ),
            "selection": {
                card.label: card.narrative_excerpt_indices
                for card in packet.dossier.knowledge_cards
                if card.narrative_excerpt_indices is not None
            },
            "primary_documents": [
                doc.model_dump(mode="json")
                for doc in packet.documents
                if doc.purpose == "primary"
            ],
        },
    )


if __name__ == "__main__":
    main()
