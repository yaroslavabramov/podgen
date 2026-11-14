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
        self.prepared_admissions = None
        if self.supports_publication_snapshot:
            self.prepare_inputs()

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

    def admission_refs(self):
        references = []
        for admission in getattr(self.materials, "runtime_admissions", ()):
            ref = admission.catalog.get("admission_ref")
            if ref is None:
                raise Conflict("runtime_admission_reference_missing")
            references.append(RuntimeRef.model_validate(ref))
        return references

    @property
    def supports_publication_snapshot(self):
        return isinstance(self.materials, ResearchMaterials)

    def prepare_inputs(self):
        references = [self.materials.shared_ref]
        if self.materials.fixed_ref is not None:
            references.append(self.materials.fixed_ref)
        admissions = tuple(self.admission_refs())
        if self.prepared_admissions != admissions:
            self.journal.execution.prepare(self.command.run_id, generation=references, evaluation=admissions)
            self.prepared_admissions = admissions
            for document in (*self.materials.documents, *self.materials.runtime_admissions):
                bind_text = getattr(getattr(document, "text", None), "bind_execution", None)
                if bind_text is not None:
                    bind_text(self.journal.execution, self.command.run_id)
        return self.journal.execution.sets[self.command.run_id]

    def authorize_publication(self, tx):
        """Live action authorization shares the publication snapshot."""
        self.checkpoint()
        if self._identity() != self.frozen_identity:
            raise Conflict("frozen_materials_changed")
        if tuple(self.admission_refs()) != self.prepared_admissions:
            raise Conflict("execution_admissions_not_prepared")
        from podgen.persistence.execution.live import authorize

        row = self.journal.execution.sets[self.command.run_id]
        authorize(self.journal.repo, tx, self.command.project_id, row["set_id"], action=True)
        self.checkpoint()

    def __call__(self):
        self.checkpoint()
        if not isinstance(self.materials, ResearchMaterials):
            if digest(self.loader(self.command).as_payload()) != self.manifest["materials_hash"]:
                raise Conflict("frozen_materials_changed")
            return
        if self._identity() != self.frozen_identity:
            raise Conflict("frozen_materials_changed")
        self.prepare_inputs()
        from podgen.persistence.execution.live import authorize

        row = self.journal.execution.sets[self.command.run_id]
        self.journal.execution.publish(
            lambda tx: authorize(self.journal.repo, tx, self.command.project_id, row["set_id"], action=True)
        )
        self.checkpoint()
