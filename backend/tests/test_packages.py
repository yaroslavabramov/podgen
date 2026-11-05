import copy
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.domain.packages import ArtifactPackage, validate_package
from podgen.persistence.database import one
from podgen.persistence.packages import PackageImporter


def package():
    return dict(
        format="podgen-fixture-v1",
        package_id=str(uuid4()),
        test_data=True,
        description="ТЕСТОВЫЙ engineering package, не экспертный материал",
        artifacts=[
            dict(
                key="first",
                kind="ResearchNote",
                payload=dict(
                    question_refs=[],
                    title="ТЕСТОВЫЙ полный ответ",
                    answer_markdown={"$text": "note.txt"},
                    citations=[],
                    interpretations=[],
                    limitations=["SYNTHETIC TEST"],
                    uncertainty_refs=[],
                    supersedes_note_refs=[],
                ),
                input_keys=[],
                audience="project",
                identity_of=None,
            )
        ],
        runtime_records=[],
        blobs=[],
        external_refs={},
    )


def test_typed_package_preserves_full_text_and_import_retry(repo, scope, producer, tmp_path):
    text = "ТЕСТ 🛰️е\u0301\r\n" + ("Полный абзац\n" * 20000) + "ПОСЛЕДНЯЯ СТРОКА"
    (tmp_path / "note.txt").write_bytes(text.encode())
    p = ArtifactPackage.model_validate(package())
    assert validate_package(p, tmp_path)["artifacts"] == 1
    importer = PackageImporter(repo)
    first = importer.import_package(*scope, p, tmp_path, producer=producer)
    second = importer.import_package(*scope, p, tmp_path, producer=producer)
    assert first[0].ref == second[0].ref
    assert repo.get(scope[0], first[0].ref).payload["answer_markdown"] == text


def test_bad_package_rolls_back_all_artifacts(repo, scope, producer, tmp_path):
    (tmp_path / "note.txt").write_text("ТЕСТОВАЯ заметка")
    raw = package()
    broken = copy.deepcopy(raw["artifacts"][0])
    broken["key"] = "second"
    broken["payload"]["interpretations"] = [
        dict(
            text_selector=dict(field="answer_markdown", char_start=0, char_end=10000),
            basis_markers=[],
            kind="analogy",
            rationale="ТЕСТ",
            limitations=[],
        )
    ]
    raw["artifacts"].append(broken)
    p = ArtifactPackage.model_validate(raw)
    with pytest.raises(ValueError, match="note_selector_out_of_bounds"):
        PackageImporter(repo).import_package(*scope, p, tmp_path, producer=producer)
    with repo.database.transaction() as tx:
        assert (
            one(tx, "SELECT count(*) AS n FROM artifact_revisions WHERE project_id=:p", p=scope[0])["n"] == 0
        )


def test_package_cycles_unknown_refs_and_blob_corruption(tmp_path):
    (tmp_path / "note.txt").write_text("ТЕСТ")
    raw = package()
    raw["artifacts"][0]["input_keys"] = ["first"]
    with pytest.raises(ContractError, match="cyclic_package_references"):
        validate_package(ArtifactPackage.model_validate(raw), tmp_path)
    raw = package()
    raw["artifacts"][0]["payload"]["question_refs"] = [{"$artifact": "absent"}]
    with pytest.raises(ContractError, match="unknown_artifact_key"):
        validate_package(ArtifactPackage.model_validate(raw), tmp_path)
    raw = package()
    raw["blobs"] = [
        dict(key="blob", path="note.txt", media_type="text/plain", sha256="0" * 64, byte_length=4)
    ]
    with pytest.raises(ContractError, match="package_blob_integrity_error"):
        validate_package(ArtifactPackage.model_validate(raw), tmp_path)
    raw = package()
    raw["artifacts"][0]["payload"]["answer_markdown"] = {"$text": "../private.txt"}
    with pytest.raises(ContractError, match="package_path_outside_root"):
        validate_package(ArtifactPackage.model_validate(raw), tmp_path)
