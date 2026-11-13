"""Reauthorize exact report/source blobs on every load, including frozen resumes."""

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.editorial_inputs import SOURCE_REF
from podgen.writing.materials import AuthorDocument

from .contracts import PERSISTED, VERSION, ResearchPacket
from .sections import SECTIONING, partition, source_mapping


@dataclass(frozen=True)
class ResearchMaterials:
    packet: ResearchPacket
    shared_ref: RuntimeRef
    documents: tuple
    fixed: object | None = None
    fixed_ref: RuntimeRef | None = None
    authorize: Callable[[], object] | None = field(default=None, compare=False, repr=False)

    # Run-local admissions never participate in the frozen packet/material hash.
    runtime_admissions: list = field(default_factory=list, compare=False, repr=False)
    runtime_annex: dict = field(default_factory=dict, compare=False, repr=False)
    document_bindings: dict | None = field(default=None, compare=False, repr=False)

    @property
    def case_id(self):
        return self.packet.assignment.key

    @property
    def format(self):
        return self.packet.assignment.format

    @property
    def output_language(self):
        return self.packet.assignment.output_language

    @property
    def speaker_labels(self):
        return tuple(row.label for row in self.packet.assignment.speakers)

    @property
    def material_catalog(self):
        return [section.material_id for section in self.packet.sections]

    def as_payload(self):
        payload = dict(
            contract="editorial-role-packets/1",
            shared_input_ref=self.shared_ref.model_dump(mode="json"),
            editorial_input=self.packet.model_dump(mode="json"),
            fixed_input_ref=self.fixed_ref.model_dump(mode="json") if self.fixed_ref else None,
            fixed_input=self.fixed.model_dump(mode="json") if self.fixed else None,
            documents=[],
        )
        for spec, document in zip(self.packet.documents, self.documents, strict=True):
            item = dict(path=document.path, sha256=document.sha256, ref=document.ref.model_dump(mode="json"))
            if self.document_bindings is None:
                item["text"] = document.text
            else:
                item["blob"] = self.document_bindings[spec.key]["blob"]
            payload["documents"].append(item)
        if self.document_bindings is not None:
            payload["source_access"] = "content-addressed-research-text/2"
        return payload


def load_materials(repo, project, ref, *, case_id, fixed_ref=None):
    from podgen.persistence.read_authorization import read_operation

    with read_operation(repo):
        materials = _load_materials(repo, project, ref, case_id=case_id, fixed_ref=fixed_ref)
        # A remote report/plan read may have closed the first metadata snapshot.
        # Reauthorize the complete frozen input before publishing this view.
        repo.authorize_runtime_many(project, [ref] + ([fixed_ref] if fixed_ref is not None else []))
        return materials


def _load_materials(repo, project, ref, *, case_id, fixed_ref=None):
    from uuid import uuid5

    payload = repo.get_runtime(project, ref)
    if (
        payload.get("contract") != PERSISTED
        or payload.get("version") != VERSION
        or payload.get("sectioning") != SECTIONING
    ):
        raise ContractError("unsupported_research_material_contract")
    if ref.record_kind != "user_input" or ref.record_id != uuid5(
        project, "research-editorial-input:" + digest(payload)
    ):
        raise ContractError("research_material_admission_binding_mismatch")
    packet = ResearchPacket.model_validate(payload["content"])
    if packet.assignment.key != case_id:
        raise ContractError("editorial_assignment_mismatch")
    if set(payload["bindings"]) != {doc.key for doc in packet.documents}:
        raise ContractError("research_material_document_bindings_mismatch")
    lazy = payload.get("preparation", {}).get("contract") == "research-material-preparation/2"
    documents = []
    representations = {}
    for spec in packet.documents:
        row = payload["bindings"][spec.key]
        origin = SOURCE_REF.validate_python(row["ref"])
        blob = BlobRef.model_validate(row["blob"])
        if isinstance(origin, ArtifactRef):
            value = repo.get(project, origin).payload
            representations[origin] = value
            if origin.kind != "SourceRepresentation" or blob.model_dump(mode="json") != (
                value.get("canonical_text_blob") or value["content_blob"]
            ):
                raise ContractError("research_source_blob_binding_mismatch")
            raw = None if lazy else repo.read_blob(project, origin, blob)
        else:
            value = repo.get_runtime(project, origin)
            if origin != packet.report_result_ref or value.get("report_text_blob") != blob.model_dump(
                mode="json"
            ):
                raise ContractError("research_report_blob_binding_mismatch")
            raw = None if lazy else repo.read_runtime_blob(project, origin, blob)
        if (raw is not None and sha256(raw).hexdigest() != spec.sha256) or blob.sha256 != spec.sha256:
            raise ContractError("research_material_hash_mismatch")
        from podgen.persistence.text_source import verified_text

        text = verified_text(repo, project, origin, blob) if lazy else raw.decode("utf-8")
        if spec.key == "report" and lazy:
            text = str(text)
        documents.append(AuthorDocument(spec.source_path, text, spec.sha256, origin))
    loaded = {spec.key: doc.text for spec, doc in zip(packet.documents, documents, strict=True)}
    for section in packet.sections:
        if section.document not in loaded or not 0 <= section.start < section.end <= len(
            loaded[section.document]
        ):
            raise ContractError("research_section_range_mismatch")
    report = loaded["report"]
    mapping = source_mapping(report)
    numbered = {s.report_number: s for s in packet.sources if s.report_number is not None}
    if set(mapping) != set(numbered) or any(
        numbered[n].source_id != f"S{n}" or numbered[n].reported_url != row["url"]
        for n, row in mapping.items()
    ):
        raise ContractError("research_report_source_number_mismatch")
    expected = partition(
        report, sources={n: dict(url=s.reported_url, source_id=s.source_id) for n, s in numbered.items()}
    )
    if packet.sections != expected:
        raise ContractError("research_section_partition_mismatch")
    if len({s.source_id for s in packet.sources}) != len(packet.sources):
        raise ContractError("research_duplicate_source_id")
    for source in packet.sources:
        if source.document is None:
            if source.status in ("available", "partial"):
                raise ContractError("research_readable_source_missing_document")
            continue
        if source.status not in ("available", "partial") or source.document not in loaded:
            raise ContractError("research_unavailable_source_document")
        binding = payload["bindings"][source.document]
        if binding["ref"] != source.representation_ref.model_dump(mode="json"):
            raise ContractError("research_source_document_binding_mismatch")
        representation = representations[source.representation_ref]
        if representation["source_revision_ref"] != source.source_revision_ref.model_dump(mode="json"):
            raise ContractError("research_source_representation_mismatch")

    def authorize():
        return repo.authorize_runtime(project, ref)

    materials = ResearchMaterials(
        packet,
        ref,
        tuple(documents),
        authorize=authorize,
        document_bindings=payload["bindings"] if lazy else None,
    )
    if fixed_ref is not None:
        from podgen.runtime.evaluation.reusable_plan import load_approved_plan

        plan = load_approved_plan(repo, project, ref, packet, fixed_ref)
        materials = ResearchMaterials(
            packet,
            ref,
            tuple(documents),
            plan,
            fixed_ref,
            authorize,
            document_bindings=payload["bindings"] if lazy else None,
        )
    return materials
