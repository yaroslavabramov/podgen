"""Live namespace, audience, catalog and privacy checks for every read."""

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import AccessDenied

from .database import one
from .repository import CONFIG_KINDS, HIDDEN_KINDS


class ScopeAccess:
    def __init__(self, repository):
        self.repo = repository

    def row(self, tx, ref):
        # Full rows are explicit business reads; never retain their payload in
        # the authorization facts or successful-node memo.
        return one(
            tx,
            "SELECT * FROM artifact_revisions WHERE revision_id=:r AND artifact_id=:a AND kind=:k",
            r=ref.revision_id,
            a=ref.artifact_id,
            k=ref.kind,
        )

    def metadata(self, tx, ref):
        from .access_facts import access_facts

        return access_facts(self.repo, tx).artifacts.row(ref)

    def publication(self, tx, consumer_project, row):
        if row["kind"] not in CONFIG_KINDS:
            raise AccessDenied()
        namespace = one(tx, "SELECT namespace_kind FROM projects WHERE project_id=:p", p=row["project_id"])
        if namespace["namespace_kind"] != "catalog":
            raise AccessDenied()
        publication = one(
            tx,
            """SELECT p.* FROM catalog_publications p WHERE p.project_id=:p AND p.revision_id=:r
                               AND NOT EXISTS(SELECT 1 FROM catalog_revocations v WHERE v.publication_id=p.publication_id)""",
            p=row["project_id"],
            r=row["revision_id"],
        )
        if (
            publication is None
            or str(consumer_project) not in publication["config"]["allowed_consumer_project_ids"]
        ):
            raise AccessDenied()
        if (
            row["kind"] == "VoiceProfile"
            and (row["catalog_voice_kind"] if "catalog_voice_kind" in row else row["payload"]["voice_kind"])
            != "stock"
        ):
            raise AccessDenied()
        return publication

    def access_invalid(self, tx, row):
        from .access_facts import AccessFacts, access_facts

        # One complete-graph privacy rule. Callers without an operation own a
        # short-lived fact set; bounded operations share their existing set.
        facts = access_facts(self.repo, tx)
        if facts is None:
            facts = AccessFacts(self.repo, tx)
        return facts.artifacts.invalid(row)

    def resolve(self, tx, project_id, ref, seen=None):
        self.repo._acl(tx, project_id)
        row = self.metadata(tx, ref)
        if row is None:
            raise AccessDenied()
        if row["project_id"] != project_id:
            self.repo._publication(tx, project_id, row)
        if row["audience"] == "evaluator" or row["kind"] in HIDDEN_KINDS:
            role = self.repo._acl(tx, row["project_id"])
            if self.repo.purpose != "evaluation" or role not in ("evaluator", "owner", "operator"):
                raise AccessDenied()
        if seen is None and self.repo._access_invalid(tx, row):
            raise AccessDenied()
        from .access_facts import access_facts

        facts = access_facts(self.repo, tx)
        for blob in facts.artifacts.bindings[row["revision_id"]]:
            facts.blobs.check(row["project_id"], blob)
        # A pinned grant does not confer perpetual catalog access, including nested inputs.
        seen = set() if seen is None else seen
        if row["revision_id"] not in seen:
            seen.add(row["revision_id"])
            for value in row["input_refs"]:
                self.repo._authorize_artifact(tx, project_id, ArtifactRef.model_validate(value), seen)
            for edge in facts.artifacts.edges[row["revision_id"]]:
                facts.artifacts.load([edge["input_id"]])
                child = facts.artifacts.rows.get(edge["input_id"])
                if child is None or child["project_id"] != row["project_id"]:
                    raise AccessDenied()
                self.repo._authorize_artifact(
                    tx,
                    project_id,
                    ArtifactRef(
                        artifact_id=child["artifact_id"], revision_id=child["revision_id"], kind=child["kind"]
                    ),
                    seen,
                )
        for link in facts.artifacts.runtime_links[row["revision_id"]]:
            self.repo._runtime(
                tx,
                row["project_id"],
                RuntimeRef(record_id=link["record_id"], record_kind=link["record_kind"]),
            )
        return row

    def runtime(self, tx, project_id, ref):
        self.repo._acl(tx, project_id)
        from .access_facts import access_facts

        facts = access_facts(self.repo, tx)
        row = facts.runtimes.row(project_id, ref)
        if row is None or (
            row["audience"] == "evaluator"
            and (
                self.repo.purpose != "evaluation"
                or self.repo._acl(tx, project_id) not in ("owner", "evaluator", "operator")
            )
        ):
            raise AccessDenied()
        references = facts.runtimes.references[project_id, ref.record_id]
        for linked in references:
            if isinstance(linked, ArtifactRef):
                self.repo._authorize_artifact(tx, project_id, linked)
            elif isinstance(linked, RuntimeRef):
                self.repo._runtime(tx, project_id, linked)
            else:
                facts.blobs.check(project_id, linked)
        return {key: row[key] for key in ("project_id", "record_id", "record_kind", "audience")}
