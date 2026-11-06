"""Authorized exact source reads and excerpt/scope verification."""

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.editorial_staging import StagedEditorialFile
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.database import query
from podgen.writing.materials import AuthorDocument

from .editorial_provenance import excluded_checksums, representation_scopes, staged_scope


def read_document(repo, project, ref, *, source_path, sha256, purpose):
    if repo.purpose != "generation":
        raise AccessDenied()
    if isinstance(ref, ArtifactRef):
        if ref.kind != "SourceRepresentation" or purpose not in ("primary",):
            raise ContractError("unsupported_editorial_source")
        with repo.database.read_transaction() as tx:
            repo._acl(tx, project)
            row = (
                query(
                    tx,
                    """SELECT kind,payload->'canonical_text_blob' AS canonical,payload->'content_blob' AS content
                FROM artifact_revisions WHERE project_id=:p AND artifact_id=:a AND revision_id=:r""",
                    p=project,
                    a=ref.artifact_id,
                    r=ref.revision_id,
                )
                .mappings()
                .first()
            )
        if not row or row["kind"] != "SourceRepresentation":
            raise AccessDenied()
        blob = BlobRef.model_validate(row["canonical"] or row["content"])
        if blob.sha256 != sha256 or blob.sha256 in excluded_checksums(repo, project):
            raise AccessDenied()
        artifact = repo.get(project, ref)
        verified_blob = BlobRef.model_validate(
            artifact.payload.get("canonical_text_blob") or artifact.payload["content_blob"]
        )
        if verified_blob != blob:
            raise ContractError("editorial_source_hash_mismatch")
        raw = repo.read_blob(project, ref, blob)
    else:
        with repo.database.read_transaction() as tx:
            repo._acl(tx, project)
            row = (
                query(
                    tx,
                    "SELECT record_kind,audience,payload,payload->>'contract' AS contract FROM runtime_records WHERE project_id=:p AND record_id=:r",
                    p=project,
                    r=ref.record_id,
                )
                .mappings()
                .first()
            )
        if row and row["contract"] == "editorial-source/1":
            from .editorial_documents import read_admitted

            payload = row["payload"]
            if (
                row["record_kind"] != "user_input"
                or row["audience"] != "project"
                or payload.get("source_path") != source_path
                or payload.get("purpose") != purpose
            ):
                raise AccessDenied()
            if purpose != "fixed_plan" and sha256 in excluded_checksums(repo, project):
                raise AccessDenied()
            raw, _ = read_admitted(
                repo, project, ref, payload, path=source_path, checksum=sha256, purpose=purpose
            )
            return AuthorDocument(source_path, raw.decode("utf-8"), sha256, ref)
        # Inspect only type/path/hash metadata before opening a potential excluded source body.
        with repo.database.transaction() as tx:
            repo._acl(tx, project)
            rows = list(
                query(
                    tx,
                    """SELECT record_id,audience,payload->>'source_path' AS path,
              payload->>'material_kind' AS kind,payload->'original_blob'->>'sha256' AS checksum
              FROM runtime_records WHERE project_id=:p AND
              (record_id=:r OR payload->>'source_path' LIKE 'author/%/plan.md' OR
               payload->>'material_kind' IN ('script_draft','script_final','evaluator_material'))""",
                    p=project,
                    r=ref.record_id,
                ).mappings()
            )
        row = next((r for r in rows if r["record_id"] == ref.record_id), None)
        blocked = {
            r["checksum"]
            for r in rows
            if r["kind"] in ("script_draft", "script_final", "evaluator_material")
            or r["path"]
            and r["path"].startswith("author/")
            and r["path"].endswith("/plan.md")
        }
        if not row or row["audience"] != "project" or row["path"] != source_path or row["checksum"] != sha256:
            raise AccessDenied()
        if purpose != "fixed_plan" and sha256 in blocked:
            raise AccessDenied()
        expected = "source_material" if purpose == "primary" else "author_input"
        if row["kind"] != expected:
            raise ContractError("editorial_source_purpose_mismatch")
        staged_scope(repo, project, ref, source_path, purpose)
        file = StagedEditorialFile.model_validate(repo.get_runtime(project, ref))
        blob = file.original_blob
        raw = repo.read_runtime_blob(project, ref, blob)
        if file.full_text is None or file.full_text.encode("utf-8") != raw:
            raise ContractError("editorial_source_text_mismatch")
    if blob.sha256 != sha256:
        raise ContractError("editorial_source_hash_mismatch")
    return AuthorDocument(source_path, raw.decode("utf-8"), sha256, ref)


def validate_bindings(repo, project, packet, docs, *, fixed_document=None):
    scopes = set()
    for spec in packet.documents:
        doc = docs[spec.key]
        if isinstance(doc.ref, RuntimeRef):
            payload = repo.get_runtime(project, doc.ref)
            if payload.get("contract") == "editorial-source/1":
                from .editorial_documents import read_admitted

                _, scope = read_admitted(
                    repo, project, doc.ref, payload, path=doc.path, checksum=doc.sha256, purpose=spec.purpose
                )
                scopes.add(scope)
            else:
                scopes.add(staged_scope(repo, project, doc.ref, doc.path, spec.purpose))
        elif isinstance(doc.ref, ArtifactRef):
            scopes.update(representation_scopes(repo, project, doc.sha256))
    if fixed_document is not None and isinstance(fixed_document.ref, RuntimeRef):
        payload = repo.get_runtime(project, fixed_document.ref)
        if payload.get("contract") == "editorial-source/1":
            from .editorial_documents import read_admitted

            _, scope = read_admitted(
                repo,
                project,
                fixed_document.ref,
                payload,
                path=fixed_document.path,
                checksum=fixed_document.sha256,
                purpose="fixed_plan",
            )
            scopes.add(scope)
        else:
            scopes.add(staged_scope(repo, project, fixed_document.ref, fixed_document.path, "fixed_plan"))
    if len(scopes) > 1:
        raise AccessDenied()
    for card in packet.dossier.knowledge_cards:
        for quote in card.excerpts:
            text = docs[quote.document].text
            if not 0 <= quote.start < quote.end <= len(text) or text[quote.start : quote.end] != quote.text:
                raise ContractError("editorial_excerpt_mismatch", card.card_id or card.label)
