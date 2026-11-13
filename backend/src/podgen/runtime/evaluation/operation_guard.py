"""Fresh access checks over already frozen inputs, without reconstructing the corpus."""

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.research.materials.loading import ResearchMaterials


class OperationGuard:
    def __init__(self, *, generation, journal, command, manifest, materials, loader, checkpoint):
        self.generation, self.journal = generation, journal
        self.command, self.manifest, self.materials = command, manifest, materials
        self.loader, self.checkpoint = loader, checkpoint
        self.frozen_identity = self._identity() if isinstance(materials, ResearchMaterials) else None

    def _identity(self):
        # Bind immutable descriptors, not corpus text. Content bytes are verified
        # by their own read boundary; packet mutation must not change this run.
        return digest(
            dict(
                packet=self.materials.packet.model_dump(mode="json"),
                shared_ref=self.materials.shared_ref.model_dump(mode="json"),
                fixed_ref=self.materials.fixed_ref.model_dump(mode="json")
                if self.materials.fixed_ref
                else None,
                bindings=self.materials.document_bindings,
                documents=[
                    dict(path=doc.path, sha256=doc.sha256, ref=doc.ref.model_dump(mode="json"))
                    for doc in self.materials.documents
                ],
            )
        )

    def __call__(self):
        self.checkpoint()
        if not isinstance(self.materials, ResearchMaterials):
            if digest(self.loader(self.command).as_payload()) != self.manifest["materials_hash"]:
                raise Conflict("frozen_materials_changed")
            return
        if self._identity() != self.frozen_identity:
            raise Conflict("frozen_materials_changed")
        # The initial load validated content-addressed immutable descriptors and
        # the complete packet/hash. Boundaries reauthorize those same references,
        # including a fixed approved plan and every admitted runtime dependency.
        from podgen.persistence.read_authorization import read_operation

        project = self.command.project_id
        with read_operation(self.generation):
            references = [self.materials.shared_ref]
            if self.materials.fixed_ref is not None:
                references.append(self.materials.fixed_ref)
            self.generation.authorize_runtime_many(project, references)
        with self.journal.read_operation():
            references = []
            for admission in self.materials.runtime_admissions:
                ref = admission.catalog.get("admission_ref")
                if ref is None:
                    raise Conflict("runtime_admission_reference_missing")
                references.append(RuntimeRef.model_validate(ref))
            self.journal.repo.authorize_runtime_many(project, references)
        self.checkpoint()
