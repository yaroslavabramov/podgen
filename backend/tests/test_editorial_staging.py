import os
from pathlib import Path

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.persistence.editorial_staging import EditorialStaging, snapshot
from podgen.persistence.repository import Repository

ROOT = Path(
    os.environ.get(
        "PODGEN_TEST_EDITORIAL_ROOT",
        Path(__file__).resolve().parents[2] / "evals/cases/development/wp02-batch-01",
    )
)


def test_current_real_editorial_packages_full_roundtrip_and_generation_isolation(repo, scope):
    report = EditorialStaging(repo).import_directory(scope[0], ROOT)
    files = snapshot(ROOT)
    assert report["semantic_acceptance"] is False and report["production_ready"] is False
    assert report["provenance"]["exact_model_id"] is None
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    for case, ref in report["author_inputs"].items():
        visible = generation.get_runtime(scope[0], RuntimeRef.model_validate(ref))
        assert visible["case_id"] == case
        for material in visible["input_refs"]:
            value = generation.get_runtime(scope[0], RuntimeRef.model_validate(material))
            assert value["source_path"].startswith((f"author/{case}/", "sources/"))
            assert "evaluator-only/" not in value["source_path"]
    with pytest.raises(AccessDenied):
        generation.get_runtime(scope[0], RuntimeRef.model_validate(report["evaluator_index"]))
    for path, ref in report["file_refs"].items():
        value = repo.get_runtime(scope[0], RuntimeRef.model_validate(ref))
        if value["full_text"] is not None:
            assert value["full_text"].encode() == files[path]
        if path.startswith("evaluator-only/"):
            with pytest.raises(AccessDenied):
                generation.get_runtime(scope[0], RuntimeRef.model_validate(ref))
        if path.endswith(("/draft.txt", "/final.txt")):
            assert value["staged_spoken_view"]
            for item in value["staged_spoken_view"]:
                assert item["text"] == value["full_text"][item["char_start"] : item["char_end"]]
    with repo.database.transaction() as tx:
        before = one(tx, "SELECT count(*) AS n FROM runtime_records WHERE project_id=:p", p=scope[0])["n"]
        assert (
            one(tx, "SELECT count(*) AS n FROM acceptance_records WHERE project_id=:p", p=scope[0])["n"] == 0
        )
        assert (
            one(tx, "SELECT count(*) AS n FROM artifact_revisions WHERE project_id=:p", p=scope[0])["n"] == 0
        )
    assert EditorialStaging(repo).import_directory(scope[0], ROOT) == report
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM runtime_records WHERE project_id=:p", p=scope[0])["n"]
            == before
        )


@pytest.mark.parametrize("mutation", ["changed-final", "extra-source", "missing-inventory"])
def test_frozen_inventory_mismatch_publishes_nothing(repo, scope, tmp_path, mutation):
    import hashlib
    import json

    from podgen.domain.errors import ContractError
    from podgen.persistence.editorial_staging import CASE_SOURCES

    for case, source in CASE_SOURCES.items():
        files = {
            f"author/{case}/brief.md": "brief",
            f"author/{case}/dossier.md": "full dossier",
            f"author/{case}/plan.md": "plan",
            f"evaluator-only/{case}/draft.txt": "ВЕДУЩИЙ: полный черновик\n",
            f"evaluator-only/{case}/final.txt": "ВЕДУЩИЙ: полный финал\n",
            f"sources/{source}/evidence.json": "[]",
        }
        for path, text in files.items():
            target = tmp_path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    (tmp_path / "PROVENANCE.md").write_text("SYNTHETIC regression package")
    inventory = {
        p.relative_to(tmp_path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    (tmp_path / "inventory.sha256.json").write_text(json.dumps(inventory))
    if mutation == "changed-final":
        (tmp_path / "evaluator-only/gps-clock/final.txt").write_text("ВЕДУЩИЙ: modified\n")
    if mutation == "extra-source":
        (tmp_path / "sources/gps/unlisted.txt").write_text("unlisted source must not reach author")
    if mutation == "missing-inventory":
        (tmp_path / "inventory.sha256.json").unlink()
    with pytest.raises(ContractError, match="staging_inventory_"):
        EditorialStaging(repo).import_directory(scope[0], tmp_path)
    with repo.database.transaction() as tx:
        for table in ("runtime_records", "blobs", "artifact_revisions", "acceptance_records"):
            assert one(tx, f"SELECT count(*) AS n FROM {table} WHERE project_id=:p", p=scope[0])["n"] == 0
