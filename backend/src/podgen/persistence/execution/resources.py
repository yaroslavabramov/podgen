"""Derive flat conditions from a complete successful authorization, never permissions."""

from uuid import UUID

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.persistence.access_facts import access_facts
from podgen.persistence.database import query
from podgen.persistence.read_authorization import authorized_read
from podgen.persistence.repository import HIDDEN_KINDS, Repository
from podgen.persistence.scope import ScopeAccess


class Resources:
    def __init__(self, project):
        self.project, self.rows = project, {}

    def add(self, purpose, kind, owner, identity, binding, *, read=True, privacy=True):
        key = purpose, kind, owner, str(identity)
        row = self.rows.setdefault(
            key,
            dict(
                purpose=purpose,
                resource_kind=kind,
                owner_project_id=owner,
                identity=str(identity),
                binding=binding,
                read_required=False,
                privacy_required=False,
            ),
        )
        row["read_required"] |= read
        row["privacy_required"] |= privacy

    def member(self, purpose, owner, evaluator=False):
        requirement = "evaluator" if evaluator else "member"
        self.add(purpose, "membership", owner, requirement, {}, privacy=False)

    def blob(self, purpose, owner, ref, *, read, source=None):
        if read and source is not None:
            self.add(
                purpose,
                "binding",
                owner,
                digest(dict(ref=source.model_dump(mode="json"), blob=ref.model_dump(mode="json"))),
                dict(ref=source.model_dump(mode="json"), blob=ref.model_dump(mode="json")),
                privacy=False,
            )
        self.add(purpose, "blob", owner, ref.storage_key, ref.model_dump(mode="json"), read=read)

    def collect(self, reader, tx):
        purpose = reader.purpose
        facts = access_facts(reader, tx)
        self.member(purpose, self.project)
        for row in facts.artifacts.rows.values():
            ref = ArtifactRef(**{k: row[k] for k in ("artifact_id", "revision_id", "kind")})
            self.add(
                purpose,
                "artifact",
                row["project_id"],
                ref.revision_id,
                ref.model_dump(mode="json"),
                read=False,
            )
            for blob in facts.artifacts.bindings[row["revision_id"]]:
                self.blob(purpose, row["project_id"], blob, read=False)
        state = reader._read_authorization.get()
        for key, row in tuple(state.completed.items()):
            if key[0] == "artifact":
                consumer = key[1]
                owner = row["project_id"]
                ref = ArtifactRef(**{k: row[k] for k in ("artifact_id", "revision_id", "kind")})
                self.add(purpose, "artifact", owner, ref.revision_id, ref.model_dump(mode="json"))
                if row["audience"] == "evaluator" or row["kind"] in HIDDEN_KINDS:
                    self.member(purpose, owner, evaluator=True)
                if owner != consumer:
                    publication = ScopeAccess(reader).publication(tx, consumer, row)
                    self.add(
                        purpose,
                        "catalog",
                        owner,
                        publication["publication_id"],
                        dict(
                            publication_id=str(publication["publication_id"]),
                            revision_id=str(row["revision_id"]),
                            consumer_project_id=str(consumer),
                        ),
                        privacy=False,
                    )
                for blob in facts.artifacts.bindings[row["revision_id"]]:
                    self.blob(purpose, owner, blob, read=True, source=ref)
            elif key[0] == "runtime":
                owner, kind, identity = key[1:]
                self.member(purpose, owner, evaluator=row["audience"] == "evaluator")
                ref = RuntimeRef(record_kind=kind, record_id=identity)
                self.add(purpose, "runtime", owner, identity, ref.model_dump(mode="json"))
                for linked in facts.runtimes.references.get((owner, identity), ()):
                    if isinstance(linked, BlobRef):
                        self.blob(purpose, owner, linked, read=True, source=ref)
            elif key[0] == "execution_inputs":
                # A semantic root can be an earlier bound execution output. Its
                # already derived conditions are metadata, not a saved grant.
                for resource in query(
                    tx,
                    """SELECT * FROM execution_input_resources
                    WHERE project_id=:p AND set_id=:s AND purpose='evaluation'""",
                    p=key[1],
                    s=key[2],
                    purpose=purpose,
                ).mappings():
                    self.add(
                        purpose,
                        resource["resource_kind"],
                        resource["owner_project_id"],
                        resource["identity"],
                        resource["binding"],
                        read=resource["read_required"],
                        privacy=resource["privacy_required"],
                    )


def compile_resources(repo, tx, project, roots):
    collected = Resources(project)
    for purpose in ("evaluation", "generation"):
        selected = [root["ref"] for root in roots if purpose == "evaluation" or root["purpose"] == purpose]
        if not selected:
            continue
        reader = Repository(repo.database, repo.objects, repo.actor_id, purpose=purpose)
        with authorized_read(reader, tx):
            facts = access_facts(reader, tx)
            artifacts, runtimes, blobs = [], [], []
            for value in selected:
                if "revision_id" in value:
                    artifacts.append(ArtifactRef.model_validate(value))
                elif "record_id" in value:
                    runtimes.append(RuntimeRef.model_validate(value))
                else:
                    blobs.append(BlobRef.model_validate(value))
            facts.artifacts.expand([ref.revision_id for ref in artifacts])
            facts.runtimes.prefetch(project, runtimes)
            facts.prefetch_runtime_links()
            for ref in artifacts:
                reader._authorize_artifact(tx, project, ref)
            for ref in runtimes:
                reader._runtime(tx, project, ref)
            for ref in blobs:
                facts.blobs.check(project, ref)
                collected.blob(purpose, project, ref, read=True)
            collected.collect(reader, tx)
    owners = {project, *(row["owner_project_id"] for row in collected.rows.values())}
    versions = {
        str(row["project_id"]): row["validity_version"]
        for row in query(
            tx,
            "SELECT project_id,validity_version FROM projects WHERE project_id=ANY(CAST(:ids AS uuid[]))",
            ids=list(owners),
        ).mappings()
    }
    assert set(map(UUID, versions)) == owners
    return list(collected.rows.values()), versions
