"""Resolve staged source scope from importer identity, never from caller-supplied type tags."""

from uuid import NAMESPACE_URL, uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied

from .database import query
from .editorial_staging import author_case
from .writing_sources import own_plan_sources


def staged_scope(repo, project, ref, path, purpose):
    case = author_case(path)
    if case is None:
        raise AccessDenied()
    with repo.database.read_transaction() as tx:
        repo._acl(tx, project)
        indices = list(
            query(
                tx,
                """SELECT record_id,payload->>'package_hash' AS checksum
            FROM runtime_records WHERE project_id=:p AND payload->>'contract'='editorial-staging-evaluator-v1'""",
                p=project,
            ).mappings()
        )
    for index in indices:
        for version in ("1.0", "1.1"):
            identity = uuid5(NAMESPACE_URL, f"podgen-editorial-staging-import-{version}:" + index["checksum"])
            if index["record_id"] != uuid5(identity, "evaluator") or ref.record_id != uuid5(identity, path):
                continue
            root = RuntimeRef(record_kind="user_input", record_id=uuid5(identity, "author:" + case))
            allowed = own_plan_sources(repo, project, root, case)
            if purpose == "fixed_plan":
                if path != f"author/{case}/plan.md":
                    raise AccessDenied()
            elif allowed.get(path) != ref:
                raise AccessDenied()
            if purpose == "primary" and not path.endswith(".txt"):
                raise AccessDenied()
            if purpose in ("brief", "dossier") and path != f"author/{case}/{purpose}.md":
                raise AccessDenied()
            return (str(identity), case)
    raise AccessDenied()


def excluded_checksums(repo, project):
    with repo.database.read_transaction() as tx:
        repo._acl(tx, project)
        return {
            row["checksum"]
            for row in query(
                tx,
                """SELECT COALESCE(payload->'original_blob'->>'sha256',payload->'content_blob'->>'sha256') AS checksum
            FROM runtime_records WHERE project_id=:p AND (payload->>'source_path' LIKE 'author/%/plan.md'
            OR payload->>'material_kind' IN ('script_draft','script_final','evaluator_material') OR payload->>'purpose'='fixed_plan')""",
                p=project,
            ).mappings()
        }


def representation_scopes(repo, project, checksum):
    """A canonical wrapper cannot remove a known frozen representation's corpus provenance."""
    with repo.database.read_transaction() as tx:
        repo._acl(tx, project)
        rows = list(
            query(
                tx,
                """SELECT record_id,payload->>'source_path' AS path FROM runtime_records
            WHERE project_id=:p AND payload->'original_blob'->>'sha256'=:sha
            AND payload->>'material_kind'='source_material'""",
                p=project,
                sha=checksum,
            ).mappings()
        )
    scopes = set()
    for row in rows:
        if not row["path"] or not row["path"].endswith(".txt"):
            continue
        try:
            scope = staged_scope(
                repo,
                project,
                RuntimeRef(record_kind="user_input", record_id=row["record_id"]),
                row["path"],
                "primary",
            )
        except AccessDenied:
            continue
        scopes.add(scope)
    return scopes
