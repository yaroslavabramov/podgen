"""Direct journal ownership and fresh rights checks, never configuration traversal."""

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.domain.references import walk_values

from .acquisition_contracts import OriginalReceipt, extraction_receipt
from .acquisition_policy import SOURCE, JournalBinding
from .database import bounded_rows, query


def prepare(repo, tx, project, batch, bodies, records):
    bindings = [node.journal for node in batch.runtimes if node.journal is not None]
    roots = {binding.reference().record_id for binding in bindings}
    roots.update(
        JournalBinding(binding.operation_id, "request").reference().record_id for binding in bindings
    )
    controls = {}
    for node in batch.artifacts:
        if node.reference_policy != "legacy/1":
            if node.reference_policy != SOURCE or node.kind != "SourceRevision":
                raise ContractError("artifact_reference_policy_invalid")
            ref = RuntimeRef.model_validate(bodies[node.key]["acquisition_record_ref"])
            controls[node.key] = {ref}
            roots.add(ref.record_id)
            for _, path, linked in walk_values(bodies[node.key].get("source_metadata_evidence", [])):
                if path.endswith("/extraction_record_ref"):
                    if not isinstance(linked, RuntimeRef) or linked.record_kind != "acquisition_record":
                        raise ContractError("source_extraction_receipt_mismatch")
                    controls[node.key].add(linked)
                    roots.add(linked.record_id)
    for node in batch.runtimes:
        if node.journal is not None:
            for _, _, ref in walk_values(bodies[node.key]):
                if isinstance(ref, RuntimeRef):
                    roots.add(ref.record_id)
    rows = (
        {
            row["record_id"]: row
            for row in query(
                tx,
                "SELECT * FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids)",
                p=project,
                ids=list(roots - records.keys()),
            ).mappings()
        }
        if roots - records.keys()
        else {}
    )
    rows.update(records)
    parent_requests = set()
    for node in batch.artifacts:
        for ref in controls.get(node.key, ()):
            row = rows.get(ref.record_id)
            if row is not None and "request_ref" in row["payload"]:
                parent_requests.add(RuntimeRef.model_validate(row["payload"]["request_ref"]).record_id)
    rows.update(
        {
            row["record_id"]: row
            for row in bounded_rows(
                tx,
                "SELECT * FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids)",
                parent_requests - rows.keys(),
                p=project,
            )
        }
    )
    required = []
    reprocess_sources = {}
    for binding in bindings:
        if not isinstance(binding, JournalBinding):
            raise ContractError("acquisition_journal_binding_invalid")
        request = rows.get(JournalBinding(binding.operation_id, "request").reference().record_id)
        if (
            request is None
            or request["record_kind"] != "acquisition_record"
            or request["audience"] != "project"
        ):
            raise AccessDenied()
        command = request["payload"]
        if command.get("operation_id") != str(binding.operation_id):
            raise ContractError("acquisition_request_owner_mismatch")
        if "rights" in command:
            required.extend(
                ArtifactRef.model_validate(command["rights"][field])
                for field in ("storage_policy_ref", "publication_policy_ref")
            )
        elif "source_revision_ref" in command:
            ref = ArtifactRef.model_validate(command["source_revision_ref"])
            required.append(ref)
            row = repo._resolve(tx, project, ref)
            reprocess_sources[binding.operation_id] = row["payload"]
        else:
            raise ContractError("acquisition_request_contract_invalid")
        if binding.stage == "original":
            payload = bodies[next(node.key for node in batch.runtimes if node.journal == binding)]
            if payload.get("role") != binding.role:
                raise ContractError("acquisition_original_role_mismatch")
        if binding.stage == "extracted":
            payload = bodies[next(node.key for node in batch.runtimes if node.journal == binding)]
            original = RuntimeRef.model_validate(payload["original_ref"])
            expected = JournalBinding(binding.operation_id, "original", binding.role).reference()
            if "source_revision_ref" in command:
                expected = RuntimeRef.model_validate(
                    reprocess_sources[binding.operation_id]["acquisition_record_ref"]
                )
            if original != expected:
                raise ContractError("acquisition_journal_parent_mismatch")
            parent = rows.get(original.record_id)
            if (
                parent is None
                or parent["record_kind"] != "acquisition_record"
                or parent["audience"] != "project"
            ):
                raise AccessDenied()
            if "original_blob" not in parent["payload"]:
                raise ContractError("acquisition_original_contract_invalid")
            required.append(BlobRef.model_validate(parent["payload"]["original_blob"]))
    for node in batch.artifacts:
        control_refs = controls.get(node.key)
        if control_refs is None:
            continue
        ref = RuntimeRef.model_validate(bodies[node.key]["acquisition_record_ref"])
        row = rows.get(ref.record_id)
        if row is None or row["record_kind"] != "acquisition_record" or row["audience"] != "project":
            raise AccessDenied()
        if ref.record_kind != row["record_kind"]:
            raise ContractError("source_acquisition_receipt_mismatch")
        payload = row["payload"]
        try:
            receipt = OriginalReceipt.model_validate(payload)
        except ValueError as error:
            raise ContractError("source_acquisition_receipt_mismatch") from error
        source = bodies[node.key]
        if any(
            source[field] != getattr(receipt, attribute)
            for field, attribute in (
                ("requested_uri", "requested_url"),
                ("resolved_uri", "resolved_url"),
                ("acquired_at", "acquired_at"),
                ("acquisition_kind", "acquisition_kind"),
            )
        ) or source["response_metadata"] != dict(
            status=receipt.status_code,
            content_type=receipt.headers.get("content-type", "application/octet-stream"),
            etag=receipt.headers.get("etag"),
            last_modified=receipt.headers.get("last-modified"),
        ):
            raise ContractError("source_acquisition_receipt_mismatch")
        request_row = rows.get(receipt.request_ref.record_id)
        if (
            request_row is None
            or request_row["record_kind"] != receipt.request_ref.record_kind
            or request_row["audience"] != "project"
        ):
            raise AccessDenied()
        original_request = request_row["payload"]
        if "rights" in original_request and source["rights"] != original_request["rights"]:
            raise ContractError("source_acquisition_rights_mismatch")
        if (
            payload.get("contract") != "source-received-original/1"
            or payload.get("original_blob") != bodies[node.key]["original_blob"]
        ):
            raise ContractError("source_acquisition_receipt_mismatch")
        required.append(BlobRef.model_validate(payload["original_blob"]))
        for linked in control_refs - {ref}:
            extraction = rows.get(linked.record_id)
            if (
                extraction is None
                or extraction["record_kind"] != linked.record_kind
                or extraction["audience"] != "project"
            ):
                raise AccessDenied()
            try:
                extracted_receipt = extraction_receipt(repo, tx, project, extraction, ref, receipt)
            except ValueError as error:
                raise ContractError("source_extraction_receipt_mismatch") from error
            if extracted_receipt.original_ref != ref:
                raise ContractError("source_extraction_receipt_mismatch")
            for evidence in bodies[node.key]["source_metadata_evidence"]:
                if evidence.get("extraction_record_ref") != linked.model_dump(mode="json"):
                    continue
                if evidence.get("extractor") != extracted_receipt.metadata.name or (
                    "title" in evidence
                    and evidence["title"] != extracted_receipt.metadata.title
                    or "published_date" in evidence
                    and evidence["published_date"] != extracted_receipt.metadata.published_date
                ):
                    raise ContractError("source_extraction_metadata_mismatch")
            required.append(extracted_receipt.text_blob)
    return rows, controls, required
