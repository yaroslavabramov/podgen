import hashlib
import json
from pathlib import Path

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.editorial_staging import CASE_SOURCES, EditorialStaging
from podgen.persistence.repository import Repository
from podgen.writing.materials import load_author_materials

ROOT = Path(__file__).resolve().parents[2] / "evals/cases/development/wp02-batch-01"


def test_generation_reads_complete_author_text_and_no_evaluator_material(repo, scope):
    project, _ = scope
    report = EditorialStaging(repo).import_directory(project, ROOT)
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    for case, source in CASE_SOURCES.items():
        ref = RuntimeRef.model_validate(report["author_inputs"][case])
        materials = load_author_materials(generation, project, ref, case_id=case)
        sources = f"sources/{source}"
        expected_paths = {f"author/{case}/{name}" for name in ("brief.md", "dossier.md", "plan.md")}
        expected_paths.update((f"{sources}/README.md", f"{sources}/evidence.json"))
        for row in json.loads((ROOT / sources / "evidence.json").read_bytes()):
            expected_paths.add(f"{sources}/{row['representation']}")
        model_documents = materials.as_payload()["documents"]
        assert len(model_documents) == len(expected_paths)
        assert {doc["path"] for doc in model_documents} == expected_paths
        for doc in model_documents:
            original = (ROOT / doc["path"]).read_bytes()
            assert doc["text"].encode() == original
            assert doc["sha256"] == hashlib.sha256(original).hexdigest()
        with pytest.raises(AccessDenied):
            load_author_materials(repo, project, ref, case_id=case)

    with pytest.raises(AccessDenied):
        snow_ref = RuntimeRef.model_validate(report["author_inputs"]["snow-pump"])
        load_author_materials(generation, project, snow_ref, case_id="gps-clock")
    for ref in (report["evaluator_index"], report["file_refs"]["evaluator-only/gps-clock/final.txt"]):
        with pytest.raises(AccessDenied):
            load_author_materials(generation, project, RuntimeRef.model_validate(ref), case_id="gps-clock")
