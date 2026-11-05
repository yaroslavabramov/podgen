from uuid import uuid4

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.contracts.runtime import CatalogPublication
from podgen.domain.errors import AccessDenied, ContractError

from .database import json_text, one, query
from .repository import CONFIG_KINDS, timestamp


class ConfigRefResolver:
    def __init__(self, repository):
        self.repo = repository

    def publish(self, project_id, ref, consumer_project_ids, *, published_assets=()):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project_id, operator=True)
            repo._lock(tx, project_id)
            row = repo._resolve(tx, project_id, ref)
            namespace = one(tx, "SELECT namespace_kind FROM projects WHERE project_id=:p", p=project_id)
            if namespace["namespace_kind"] != "catalog" or ref.kind not in CONFIG_KINDS:
                raise AccessDenied()
            if ref.kind == "VoiceProfile" and row["payload"]["voice_kind"] != "stock":
                raise AccessDenied()
            # Publish leaves before parents: the configuration closure must itself be public.
            for child in row["input_refs"]:
                child_row = repo._row(tx, ArtifactRef.model_validate(child))
                for consumer in consumer_project_ids:
                    repo._publication(tx, consumer, child_row)
            publication = CatalogPublication(
                publication_id=uuid4(),
                config_ref=ref,
                published_assets=list(published_assets),
                allowed_consumer_project_ids=consumer_project_ids,
                publisher_id=repo.actor_id,
                created_at=timestamp(),
            )
            for asset in publication.published_assets:
                repo._check_blob(tx, project_id, asset.blob_ref, public_only=True)
                repo.objects.read(project_id, asset.blob_ref)
            query(
                tx,
                """INSERT INTO catalog_publications VALUES(:id,:p,:r,CAST(:body AS json),:actor,:time)""",
                id=publication.publication_id,
                p=project_id,
                r=ref.revision_id,
                body=json_text(publication),
                actor=repo.actor_id,
                time=publication.created_at,
            )
            repo._bump(tx, project_id)
            return publication

    def revoke(self, project_id, publication_id, reason):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._acl(tx, project_id, operator=True)
            repo._lock(tx, project_id)
            if not one(
                tx,
                "SELECT 1 FROM catalog_publications WHERE publication_id=:id AND project_id=:p",
                id=publication_id,
                p=project_id,
            ):
                raise AccessDenied()
            query(
                tx,
                "INSERT INTO catalog_revocations VALUES(:id,:why,:time)",
                id=publication_id,
                why=reason,
                time=timestamp(),
            )
            repo._bump(tx, project_id)

    def resolve(self, consumer_project, ref, *, expected_kind, expected_subtype=None):
        if ref.kind != expected_kind or expected_kind not in CONFIG_KINDS:
            raise AccessDenied()
        revision = self.repo.get(consumer_project, ref)
        if (
            expected_subtype is not None
            and revision.payload.get("policy_kind", revision.payload.get("profile_kind")) != expected_subtype
        ):
            raise ContractError("configuration_subtype_mismatch")
        return revision

    def resolve_for_cell(
        self,
        consumer_project,
        ref,
        *,
        expected_kind,
        role,
        language,
        format,
        duration_seconds,
        companion_profile_refs=(),
    ):
        configuration = self.resolve(consumer_project, ref, expected_kind=expected_kind)
        projection = self.qualification(consumer_project, ref)
        companions = {json_text(r) for r in companion_profile_refs}
        for scope in projection["qualified_scopes"]:
            for cell in scope["supported_cells"]:
                interval = cell["duration_interval"]
                if (
                    role in cell["roles"]
                    and language == cell["language"]
                    and format == cell["format"]
                    and interval["min_seconds"] <= duration_seconds <= interval["max_seconds"]
                    and {json_text(r) for r in cell["companion_profile_refs"]} == companions
                ):
                    return configuration
        raise ContractError("profile_not_qualified_for_cell")

    def qualification(self, consumer_project, ref):
        """Safe backend projection: no raw results, case refs, gold or diagnostic payloads."""
        repo = self.repo
        with repo.database.transaction() as tx:
            row = repo._resolve(tx, consumer_project, ref)
            records = query(
                tx,
                """SELECT a.payload FROM acceptance_records a WHERE a.project_id=:p AND a.subject_id=:r
                                    AND a.check_kind='profile_qualification' AND a.decision='accepted'
                                    AND NOT EXISTS(SELECT 1 FROM revocations v WHERE v.project_id=a.project_id
                                                   AND v.acceptance_record_id=a.record_id)""",
                p=row["project_id"],
                r=ref.revision_id,
            ).mappings()
            safe_scopes = []
            for record in records:
                visible = []
                for cell in record["payload"]["assessment_scope"]["supported_cells"]:
                    try:
                        for companion in cell["companion_profile_refs"]:
                            repo._resolve(tx, consumer_project, ArtifactRef.model_validate(companion))
                    except AccessDenied:
                        continue
                    visible.append(cell)
                if visible:
                    safe_scopes.append(dict(supported_cells=visible))
            return dict(profile_ref=ref.model_dump(mode="json"), qualified_scopes=safe_scopes)

    def read_published_asset(self, consumer_project, ref, asset_sha256, *, role):
        repo = self.repo
        with repo.database.transaction() as tx:
            row = repo._resolve(tx, consumer_project, ref)
            publication = repo._publication(tx, consumer_project, row)
            if role == "evaluator" and (
                repo.purpose != "evaluation"
                or repo._acl(tx, consumer_project) not in ("owner", "evaluator", "operator")
            ):
                raise AccessDenied()
            for asset in CatalogPublication.model_validate(publication["config"]).published_assets:
                if asset.blob_ref.sha256 == asset_sha256 and role in asset.allowed_roles:
                    repo._check_blob(tx, row["project_id"], asset.blob_ref, public_only=True)
                    return repo.objects.read(row["project_id"], asset.blob_ref)
            raise AccessDenied()
