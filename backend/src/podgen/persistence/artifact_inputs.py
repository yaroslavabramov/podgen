"""Unique immutable reads, with every field-dependent contract checked separately."""

from contextlib import ExitStack, contextmanager

from podgen.domain.contracts.common import ArtifactRef, BlobRef
from podgen.domain.references import walk_values

from .access_facts import access_facts
from .database import query
from .read_authorization import authorized_read


class ArtifactInputs:
    def __init__(self, repo, tx, project, payload, input_refs, producer, public_reader):
        self.repo, self.tx, self.project = repo, tx, project
        self.occurrences = list(walk_values(payload))
        self.occurrences.extend(("input_refs", "", ref) for ref in input_refs)
        if producer["kind"] == "model":
            self.occurrences.append(
                ("model_profile_ref", "", ArtifactRef.model_validate(producer["profile_ref"]))
            )
        self.artifacts = tuple(
            dict.fromkeys(ref for _, _, ref in self.occurrences if isinstance(ref, ArtifactRef))
        )
        self.blobs = tuple(dict.fromkeys(ref for _, _, ref in self.occurrences if isinstance(ref, BlobRef)))
        self.runtimes = tuple(
            dict.fromkeys(
                ref for _, _, ref in self.occurrences if not isinstance(ref, (ArtifactRef, BlobRef))
            )
        )
        self.public_reader, self.rows = public_reader, {}

    @contextmanager
    def authorized(self):
        with ExitStack() as stack:
            for reader in dict.fromkeys((self.repo, self.public_reader)):
                if reader is None:
                    continue
                stack.enter_context(authorized_read(reader, self.tx))
                facts = access_facts(reader, self.tx)
                facts.artifacts.expand([ref.revision_id for ref in self.artifacts])
                facts.runtimes.prefetch(self.project, self.runtimes)
                facts.prefetch_runtime_links()
                facts.blobs.load(self.project, [ref.storage_key for ref in self.blobs])
                for ref in self.artifacts:
                    reader._authorize_artifact(self.tx, self.project, ref)
                for ref in self.runtimes:
                    reader._runtime(self.tx, self.project, ref)
                for ref in self.blobs:
                    facts.blobs.check(self.project, ref, public_only=self.public_reader is not None)
            self.load(self.artifacts)
            yield self

    def load(self, references):
        pending = list({ref.revision_id for ref in references if ref.revision_id not in self.rows})
        for offset in range(0, len(pending), 64):
            for row in query(
                self.tx,
                "SELECT * FROM artifact_revisions WHERE revision_id=ANY(:ids)",
                ids=pending[offset : offset + 64],
            ).mappings():
                self.rows[row["revision_id"]] = row

    def resolve(self, ref):
        # Binding validation may discover deeper immutable inputs. They are still
        # authorized here; only explicitly requested business bodies are retained.
        self.repo._authorize_artifact(self.tx, self.project, ref)
        if ref.revision_id not in self.rows:
            if self.public_reader is not None and self.public_reader is not self.repo:
                self.public_reader._authorize_artifact(self.tx, self.project, ref)
            self.load([ref])
        return self.rows[ref.revision_id]
