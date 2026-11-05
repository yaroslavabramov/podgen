"""Give each writing role the same versioned, text-only inputs from the generation repository."""

import json
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.editorial_staging import StagedEditorialFile
from podgen.domain.errors import AccessDenied, ContractError
from podgen.domain.frozen_evidence import verify_excerpts
from podgen.persistence.editorial_staging import CASE_SOURCES, author_case
from podgen.persistence.repository import Repository

EpisodeFormat = Literal["dialogue", "solo"]


@dataclass(frozen=True)
class CaseAssignment:
    format: EpisodeFormat
    speaker_labels: tuple[str, ...]
    output_language: str


# Controlled conditions for the two frozen briefs. Speaker labels are routing metadata.
ASSIGNMENTS = {
    "gps-clock": CaseAssignment("dialogue", ("НИНА", "ИЛЬЯ"), "ru"),
    "snow-pump": CaseAssignment("solo", ("РАССКАЗЧИК",), "ru"),
}


@dataclass(frozen=True)
class AuthorDocument:
    path: str
    text: str
    sha256: str
    ref: RuntimeRef


@dataclass(frozen=True)
class AuthorMaterials:
    case_id: str
    format: EpisodeFormat
    speaker_labels: tuple[str, ...]
    documents: tuple[AuthorDocument, ...]
    output_language: str
    own_plan: bool = False

    def as_payload(self) -> dict:
        payload = {
            "source_access": "frozen-text-v1",
            "case_id": self.case_id,
            "format": self.format,
            "speaker_labels": list(self.speaker_labels),
            "documents": [
                {"path": doc.path, "sha256": doc.sha256, "text": doc.text} for doc in self.documents
            ],
        }

        if self.own_plan:
            evidence = next(d for d in self.documents if d.path.endswith("/evidence.json"))
            payload.update(
                plan_mode="own-plan",
                provided_plan_instructions="Any frozen brief instructions conditional on a supplied plan apply only to fixed-plan. Build and use your own source-grounded plan in this condition.",
                material_catalog=[d.path for d in self.documents]
                + [r["label"] for r in json.loads(evidence.text)],
            )
        return payload


def load_author_materials(
    repo: Repository, project_id: UUID, author_ref: RuntimeRef, *, case_id: str
) -> AuthorMaterials:
    if repo.purpose != "generation" or author_ref.record_kind != "user_input":
        raise AccessDenied()
    if case_id not in ASSIGNMENTS:
        raise ContractError("unsupported_frozen_writing_case", case_id)
    manifest = repo.get_runtime(project_id, author_ref)
    if (
        manifest.get("contract") != "editorial-staging-author-v1"
        or manifest.get("case_id") != case_id
        or manifest.get("technical_fixture") is not True
        or manifest.get("semantic_acceptance") is not False
    ):
        raise AccessDenied()
    files = {}
    for linked in manifest["input_refs"]:
        ref = RuntimeRef.model_validate(linked)
        if ref.record_kind != "user_input":
            raise AccessDenied()
        file = StagedEditorialFile.model_validate(repo.get_runtime(project_id, ref))
        path = PurePosixPath(file.source_path)
        if (
            author_case(file.source_path) != case_id
            or ".." in path.parts
            or path.as_posix() != file.source_path
        ):
            raise AccessDenied()
        expected_kind = "author_input" if path.parts[0] == "author" else "source_material"
        if file.material_kind != expected_kind or file.source_path in files:
            raise ContractError("invalid_author_manifest_file", file.source_path)
        files[file.source_path] = (ref, file)

    return _load_documents(repo, project_id, case_id, lambda path: files[path], with_fixed_plan=True)


def load_own_plan_materials(repo, project_id, author_ref, *, case_id):
    from podgen.persistence.writing_sources import own_plan_sources

    allowed = own_plan_sources(repo, project_id, author_ref, case_id)

    def resolve(path):
        if path not in allowed:
            raise ContractError("own_plan_source_not_allowed", path)
        ref = allowed[path]
        file = StagedEditorialFile.model_validate(repo.get_runtime(project_id, ref))
        expected_kind = "author_input" if path.startswith("author/") else "source_material"
        if (
            file.source_path != path
            or file.material_kind != expected_kind
            or file.prior_staged_script_ref is not None
        ):
            raise AccessDenied()
        return ref, file

    return replace(_load_documents(repo, project_id, case_id, resolve, with_fixed_plan=False), own_plan=True)


def _load_documents(repo, project_id, case_id, resolve, *, with_fixed_plan):
    documents = {}

    def read_document(path: str) -> AuthorDocument:
        if path not in documents:
            try:
                ref, file = resolve(path)
            except KeyError:
                raise ContractError("author_document_missing", path) from None
            raw = repo.read_runtime_blob(project_id, ref, file.original_blob)
            if file.full_text is None or not file.full_text.strip() or raw != file.full_text.encode("utf-8"):
                raise ContractError("author_text_mismatch", path)
            documents[path] = AuthorDocument(path, file.full_text, file.original_blob.sha256, ref)
        return documents[path]

    for name in ("brief.md", "dossier.md", "plan.md") if with_fixed_plan else ("brief.md", "dossier.md"):
        read_document(f"author/{case_id}/{name}")
    source_dir = f"sources/{CASE_SOURCES[case_id]}"
    read_document(f"{source_dir}/README.md")
    excerpts = json.loads(read_document(f"{source_dir}/evidence.json").text)
    if not excerpts:
        raise ContractError("empty_frozen_evidence_index", case_id)

    def read_representation(name: str) -> str:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name or path.suffix != ".txt":
            raise ContractError("invalid_source_representation_path", name)
        return read_document(f"{source_dir}/{name}").text

    verify_excerpts(excerpts, read_representation)
    assignment = ASSIGNMENTS[case_id]
    return AuthorMaterials(
        case_id,
        assignment.format,
        assignment.speaker_labels,
        tuple(documents.values()),
        assignment.output_language,
    )
