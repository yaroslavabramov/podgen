"""Admit versioned editorial data against exact, authorized source bytes; retain the complete originals."""

import json
from dataclasses import dataclass
from io import BytesIO
from uuid import uuid5

from pydantic import TypeAdapter

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.writing.editorial_contract import EditorialInput, FixedEditorialInput

from .editorial_packets import peek_packet
from .editorial_source_reader import read_document, validate_bindings
from .runtime_records import append_runtime

SOURCE_REF = TypeAdapter(ArtifactRef | RuntimeRef)
INPUT_CONTRACT = "editorial-role-packets/1"


def import_shared(repo, project, data, source_map, *, original_bytes=None):
    packet = EditorialInput.model_validate(data)
    generation = type(repo)(repo.database, repo.objects, repo.actor_id, purpose="generation")
    bindings = {}
    docs = {}
    for spec in packet.documents:
        if spec.source_path not in source_map:
            raise ContractError("editorial_source_mapping_missing", spec.source_path)
        ref = SOURCE_REF.validate_python(source_map[spec.source_path])
        docs[spec.key] = read_document(
            generation, project, ref, source_path=spec.source_path, sha256=spec.sha256, purpose=spec.purpose
        )
        bindings[spec.key] = ref.model_dump(mode="json")
    validate_bindings(generation, project, packet, docs)
    from podgen.writing.projections import validate_editorial_content

    validate_editorial_content(packet, docs.values())
    content = packet.model_dump(mode="json")
    raw = save_original(repo, project, data, original_bytes)
    payload = dict(
        contract="persisted-editorial-input/1",
        content=content,
        source_refs=bindings,
        original_input_blob=raw.model_dump(mode="json"),
    )
    ref = RuntimeRef(record_kind="user_input", record_id=uuid5(project, "editorial-input:" + digest(payload)))
    publish(repo, project, ref, payload)
    return ref


def import_fixed(repo, project, data, source_map, shared_ref, *, original_bytes=None):
    fixed = FixedEditorialInput.model_validate(data)
    shared = peek_packet(generation_repo(repo), project, shared_ref)
    packet = EditorialInput.model_validate(shared["content"])
    materials = load_editorial_materials(
        generation_repo(repo), project, shared_ref, case_id=packet.assignment.key
    )
    if fixed.assignment_key != packet.assignment.key or fixed.shared_input_digest != digest(
        shared["content"]
    ):
        raise ContractError("fixed_editorial_input_binding_mismatch")
    fixed.plan.validate_against(
        material_locators=[c.label for c in packet.dossier.knowledge_cards],
        speaker_ids=[s.label for s in packet.assignment.speakers],
    )
    if fixed.plan.status != "ready":
        raise ContractError("fixed_editorial_plan_not_ready")
    if fixed.document.source_path not in source_map:
        raise ContractError("editorial_source_mapping_missing")
    ref = SOURCE_REF.validate_python(source_map[fixed.document.source_path])
    generation = type(repo)(repo.database, repo.objects, repo.actor_id, purpose="generation")
    original = read_document(
        generation,
        project,
        ref,
        source_path=fixed.document.source_path,
        sha256=fixed.document.sha256,
        purpose="fixed_plan",
    )
    from podgen.writing.projections import validate_model_content

    validate_model_content(fixed.plan.model_dump(mode="json"), [original])
    validate_bindings(
        generation,
        project,
        packet,
        {d.key: doc for d, doc in zip(packet.documents, materials.documents, strict=True)},
        fixed_document=original,
    )
    raw = save_original(repo, project, data, original_bytes)
    payload = dict(
        original_input_blob=raw.model_dump(mode="json"),
        contract="persisted-fixed-editorial-input/1",
        content=fixed.model_dump(mode="json"),
        source_ref=ref.model_dump(mode="json"),
        shared_input_ref=shared_ref.model_dump(mode="json"),
    )
    saved = RuntimeRef(
        record_kind="user_input", record_id=uuid5(project, "fixed-editorial-input:" + digest(payload))
    )
    publish(repo, project, saved, payload)
    return saved


@dataclass(frozen=True)
class EditorialMaterials:
    packet: EditorialInput
    shared_ref: RuntimeRef
    documents: tuple
    fixed: FixedEditorialInput | None = None
    fixed_ref: RuntimeRef | None = None

    @property
    def case_id(self):
        return self.packet.assignment.key

    @property
    def format(self):
        return self.packet.assignment.format

    @property
    def speaker_labels(self):
        return tuple(s.label for s in self.packet.assignment.speakers)

    @property
    def output_language(self):
        return self.packet.assignment.output_language

    @property
    def material_catalog(self):
        return [c.label for c in self.packet.dossier.knowledge_cards]

    def as_payload(self):
        return dict(
            contract=INPUT_CONTRACT,
            shared_input_ref=self.shared_ref.model_dump(mode="json"),
            editorial_input=self.packet.model_dump(mode="json"),
            fixed_input_ref=self.fixed_ref.model_dump(mode="json") if self.fixed_ref else None,
            fixed_input=self.fixed.model_dump(mode="json") if self.fixed else None,
            documents=[
                dict(path=d.path, sha256=d.sha256, ref=d.ref.model_dump(mode="json"), text=d.text)
                for d in self.documents
            ],
        )


def load_editorial_materials(repo, project, shared_ref, *, case_id, fixed_ref=None):
    raw = peek_packet(repo, project, shared_ref)
    if raw.get("contract") != "persisted-editorial-input/1":
        raise ContractError("editorial_input_projection_required")
    packet = EditorialInput.model_validate(raw["content"])
    if packet.assignment.key != case_id:
        raise ContractError("editorial_assignment_mismatch")
    if set(raw["source_refs"]) != {spec.key for spec in packet.documents}:
        raise ContractError("editorial_input_binding_mismatch")
    docs = []
    for spec in packet.documents:
        ref = SOURCE_REF.validate_python(raw["source_refs"][spec.key])
        docs.append(
            read_document(
                repo, project, ref, source_path=spec.source_path, sha256=spec.sha256, purpose=spec.purpose
            )
        )
    validate_bindings(
        repo, project, packet, {spec.key: doc for spec, doc in zip(packet.documents, docs, strict=True)}
    )
    from podgen.writing.projections import validate_editorial_content

    validate_editorial_content(packet, docs)
    repo.get_runtime(project, shared_ref)
    fixed = None
    if fixed_ref is not None:
        saved = peek_packet(repo, project, fixed_ref, fixed=True)
        if saved.get("contract") != "persisted-fixed-editorial-input/1" or saved[
            "shared_input_ref"
        ] != shared_ref.model_dump(mode="json"):
            raise ContractError("fixed_editorial_input_binding_mismatch")
        fixed = FixedEditorialInput.model_validate(saved["content"])
        if fixed.assignment_key != packet.assignment.key or fixed.shared_input_digest != digest(
            packet.model_dump(mode="json")
        ):
            raise ContractError("fixed_editorial_input_binding_mismatch")
        source = SOURCE_REF.validate_python(saved["source_ref"])
        fixed.plan.validate_against(
            material_locators=[c.label for c in packet.dossier.knowledge_cards],
            speaker_ids=[s.label for s in packet.assignment.speakers],
        )
        if fixed.plan.status != "ready":
            raise ContractError("fixed_editorial_plan_not_ready")
        original = read_document(
            repo,
            project,
            source,
            source_path=fixed.document.source_path,
            sha256=fixed.document.sha256,
            purpose="fixed_plan",
        )
        validate_bindings(
            repo,
            project,
            packet,
            {spec.key: doc for spec, doc in zip(packet.documents, docs, strict=True)},
            fixed_document=original,
        )
        repo.get_runtime(project, fixed_ref)
        docs.append(original)
    return EditorialMaterials(packet, shared_ref, tuple(docs), fixed, fixed_ref)


def generation_repo(repo):
    return type(repo)(repo.database, repo.objects, repo.actor_id, purpose="generation")


def save_original(repo, project, data, original_bytes):
    if original_bytes is None:
        original_bytes = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    from podgen.runtime.evaluation.validation import load_unique_json

    if load_unique_json(original_bytes.decode("utf-8")) != data:
        raise ContractError("editorial_original_bytes_mismatch")
    return repo.save_blob(project, BytesIO(original_bytes), "application/json")


def publish(repo, project, ref, payload):
    with repo.database.transaction() as tx:
        repo._lock(tx, project)
        return append_runtime(repo, tx, project, ref, payload, replay=True)
