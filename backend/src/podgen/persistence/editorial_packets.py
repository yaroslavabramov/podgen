"""Inspect an admission envelope before traversing any of its source dependencies."""

from uuid import uuid5

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied, ContractError

from .database import one

SHARED = {"contract", "content", "source_refs", "original_input_blob"}
FIXED = {"contract", "content", "source_ref", "shared_input_ref", "original_input_blob"}


def peek_packet(repo, project, ref, *, fixed=False):
    with repo.database.read_transaction() as tx:
        repo._acl(tx, project)
        row = one(
            tx,
            "SELECT record_kind,audience,payload FROM runtime_records WHERE project_id=:p AND record_id=:r",
            p=project,
            r=ref.record_id,
        )
    if (
        not row
        or ref.record_kind != "user_input"
        or row["record_kind"] != "user_input"
        or row["audience"] != "project"
    ):
        raise AccessDenied()
    payload = row["payload"]
    if not fixed and payload.get("contract") == "persisted-research-editorial-input/1":
        expected = {
            "contract",
            "version",
            "sectioning",
            "content",
            "bindings",
            "preparation",
            "original_input_blob",
        }
        if set(payload) != expected or ref.record_id != uuid5(
            project, "research-editorial-input:" + digest(payload)
        ):
            raise ContractError("research_material_admission_binding_mismatch")
        return payload
    contract = "persisted-fixed-editorial-input/1" if fixed else "persisted-editorial-input/1"
    prefix = "fixed-editorial-input:" if fixed else "editorial-input:"
    if payload.get("contract") != contract:
        raise ContractError("editorial_input_projection_required")
    if set(payload) != (FIXED if fixed else SHARED) or ref.record_id != uuid5(
        project, prefix + digest(payload)
    ):
        raise ContractError("editorial_input_binding_mismatch")
    return payload
