"""Narrow source selection from an immutable, hash-bound staging package; never open excluded bodies."""

from uuid import NAMESPACE_URL, UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied

from .database import one, query
from .editorial_staging import CASE_SOURCES, author_case


def own_plan_sources(repo, project, author_ref, case):
    if repo.purpose != "generation" or author_ref.record_kind != "user_input" or case not in CASE_SOURCES:
        raise AccessDenied()
    with repo.database.transaction() as tx:
        repo._acl(tx, project)
        # Task-specific projection: traversing the broad author's refs would also open its fixed plan.
        root = one(
            tx,
            """SELECT record_kind,audience,payload->>'contract' AS contract,
          payload->>'case_id' AS case_id,payload->'input_refs' AS refs,
          ARRAY(SELECT json_object_keys(payload)) AS keys
          FROM runtime_records WHERE project_id=:p AND record_id=:r""",
            p=project,
            r=author_ref.record_id,
        )
        if (
            not root
            or root["record_kind"] != "user_input"
            or root["audience"] != "project"
            or root["contract"] != "editorial-staging-author-v1"
            or root["case_id"] != case
            or set(root["keys"])
            != {"contract", "case_id", "technical_fixture", "semantic_acceptance", "input_refs"}
        ):
            raise AccessDenied()
        package = None
        for row in query(
            tx,
            """SELECT record_id,payload->>'package_hash' AS checksum
          FROM runtime_records WHERE project_id=:p AND payload->>'contract'='editorial-staging-evaluator-v1'""",
            p=project,
        ).mappings():
            for importer in ("1.0", "1.1"):
                identity = uuid5(
                    NAMESPACE_URL, f"podgen-editorial-staging-import-{importer}:" + row["checksum"]
                )
                if row["record_id"] == uuid5(identity, "evaluator") and author_ref.record_id == uuid5(
                    identity, "author:" + case
                ):
                    package = (identity, row)
        if package is None:
            raise AccessDenied()
        identity, index = package
        rows = list(
            query(
                tx,
                """SELECT e.key AS path,e.value AS ref,r.record_kind,r.audience,
          r.payload->>'source_path' AS source_path,r.payload->'original_blob'->>'sha256' AS checksum,
          r.payload->'prior_staged_script_ref' AS prior
          FROM runtime_records i CROSS JOIN LATERAL json_each(i.payload->'files') e
          LEFT JOIN runtime_records r ON r.project_id=i.project_id AND r.record_id=(e.value->>'record_id')::uuid
          WHERE i.project_id=:p AND i.record_id=:r""",
                p=project,
                r=index["record_id"],
            ).mappings()
        )
        if not rows or digest({row["path"]: row["checksum"] for row in rows}) != index["checksum"]:
            raise AccessDenied()
        expected = []
        selected = {}
        # Exact-byte aliases stay excluded even if wrapped under an allowed path in another package.
        forbidden_hashes = {
            row["checksum"]
            for row in query(
                tx,
                """SELECT payload->'original_blob'->>'sha256' AS checksum
              FROM runtime_records WHERE project_id=:p AND
              (payload->>'source_path' LIKE 'author/%/plan.md' OR
               payload->>'material_kind' IN ('script_draft','script_final','evaluator_material'))""",
                p=project,
            ).mappings()
        }
        for row in rows:
            ref = RuntimeRef.model_validate(row["ref"])
            if (
                ref.record_kind != "user_input"
                or ref.record_id != uuid5(identity, row["path"])
                or row["record_kind"] != "user_input"
                or row["source_path"] != row["path"]
            ):
                raise AccessDenied()
            if author_case(row["path"]) == case:
                expected.append(ref.model_dump(mode="json"))
            allowed = row["path"] in (f"author/{case}/brief.md", f"author/{case}/dossier.md") or row[
                "path"
            ].startswith(f"sources/{CASE_SOURCES[case]}/")
            if allowed:
                if (
                    row["audience"] != "project"
                    or row["prior"] is not None
                    or row["checksum"] in forbidden_hashes
                ):
                    raise AccessDenied()
                selected[row["path"]] = ref

        def canonical(refs):
            if any(not isinstance(r, dict) or set(r) != {"record_kind", "record_id"} for r in refs):
                raise AccessDenied()
            return sorted((r["record_kind"], str(UUID(r["record_id"]))) for r in refs)

        if not isinstance(root["refs"], list) or canonical(root["refs"]) != canonical(expected):
            raise AccessDenied()
        return selected
