"""Exact control ownership: a run is an envelope, never a substitute for a parent."""

from uuid import UUID

from podgen.domain.errors import Conflict, ContractError
from podgen.persistence.database import query

from . import access

POINTERS = {
    "submit_entry_id": {"submit_intent", "count_receipt_submit_intent", "legacy_adopted"},
    "request_entry_id": {
        "attempt_prepared",
        "authorized_replacement",
        "count_intent",
        "count_receipt_count_intent",
        "legacy_adopted",
    },
    "provider_entry_id": {"provider_receipt", "legacy_adopted"},
    "count_entry_id": {
        "count_receipt",
        "count_receipt_submit_intent",
        "count_receipt_count_intent",
        "legacy_adopted",
    },
}


def parents(repo, tx, project, command, *, authorize=True):
    if not command.parents:
        rows = {}
    else:
        rows = {
            row["entry_id"]: dict(row)
            for row in query(
                tx,
                """SELECT entry_id,input_set_id,checkpoint_kind,step_id,attempt_id,operation_id,data,owned_objects,archive_views,owned_hashes FROM execution_entries WHERE project_id=:p AND run_id=:r
            AND entry_id=ANY(CAST(:ids AS uuid[]))""",
                p=project,
                r=command.run_id,
                ids=list(command.parents),
            ).mappings()
        }
        if set(rows) != set(command.parents):
            raise ContractError("private_control_owner_mismatch")
        if authorize:
            authorize_parents(repo, tx, project, command, rows)
    return rows


def validate(repo, tx, project, command, previous, rows):
    from .count_recovery import validate as validate_count_recovery

    validate_count_recovery(command, previous, rows)
    recovery(repo, tx, project, command, previous, rows)
    if command.kind == "attempt_response" and (
        not previous
        or previous["active_attempt_id"] != command.attempt_id
        or previous["phase"] not in ("response_completed", "tool_running")
    ):
        raise Conflict("private_role_response_without_completed_generation")
    for field, kinds in POINTERS.items():
        target = getattr(command.head, field)
        if target is None:
            continue
        if target == command.entry_id:
            kind, step, attempt = command.kind, command.step_id, command.attempt_id
            contains_request = any(
                view["payload"].get("contract") == "model-request-v1"
                and view["payload"].get("invocation_id") == str(command.step_id or command.attempt_id)
                for view in command.views
            )
        elif target in rows:
            row = rows[target]
            kind, step, attempt = row["checkpoint_kind"], row["step_id"], row["attempt_id"]
            contains_request = any(
                view.get("projection") == "physical-request/3" for view in row["archive_views"]
            )
        else:
            raise ContractError("private_control_pointer_not_bound")
        valid_kind = kind in kinds or field == "request_entry_id" and contains_request
        if not valid_kind or step != command.step_id or attempt != command.attempt_id:
            raise ContractError("private_control_pointer_mismatch")
    if command.kind == "provider_receipt":
        identity = _parent(command, rows, "submit_entry_id")
        if identity["checkpoint_kind"] not in (
            "submit_intent",
            "count_receipt_submit_intent",
            "legacy_adopted",
        ):
            raise ContractError("private_provider_receipt_without_submit")
        if identity["step_id"] != command.step_id or identity["operation_id"] != command.operation_id:
            raise ContractError("private_provider_receipt_submit_mismatch")
    if command.kind in ("tool_intent", "tool_result"):
        receipt = _parent(command, rows, "provider_entry_id")
        if (
            receipt["checkpoint_kind"] not in ("provider_receipt", "legacy_receipt")
            or receipt["step_id"] != command.step_id
            or receipt["data"].get("state", {}).get("status") != "response_completed"
        ):
            raise ContractError("private_tool_receipt_mismatch")
        proof_owner = receipt
        if command.data.get("decision_entry_id"):
            proof_owner = _parent(command, rows, "decision_entry_id")
            if proof_owner["checkpoint_kind"] != "structured_decision" or proof_owner["data"].get(
                "provider_entry_id"
            ) != str(receipt["entry_id"]):
                raise ContractError("private_structured_receipt_mismatch")
        proof = [
            row
            for row in proof_owner["data"].get("provider_calls", ())
            if (
                row["call_id"] == command.data.get("call_id")
                and row["arguments_hash"] == command.data.get("arguments_sha256")
                and row["tool_name"] == command.data.get("tool_name")
                and row["physical_invocation_id"] == str(command.step_id)
            )
        ]
        if len(proof) != 1:
            raise ContractError("private_tool_call_proof_mismatch")
    if command.kind == "structured_decision":
        receipt = _parent(command, rows, "provider_entry_id")
        if (
            receipt["checkpoint_kind"] not in ("provider_receipt", "legacy_receipt")
            or receipt["step_id"] != command.step_id
        ):
            raise ContractError("private_structured_receipt_mismatch")
    if command.kind == "nested_return":
        returned = _parent(command, rows, "return_entry_id")
        if not previous or previous["pending_parent_entry_id"] != returned["entry_id"]:
            raise Conflict("private_nested_return_mismatch")
        if returned["attempt_id"] != command.attempt_id or returned["step_id"] != command.step_id:
            raise Conflict("private_nested_return_owner_mismatch")
        if "pending_parent_entry_id" not in command.head.clear_fields:
            raise ContractError("private_nested_return_must_clear_parent")


def _parent(command, rows, field):
    try:
        return rows[UUID(command.data[field])]
    except (KeyError, TypeError, ValueError) as error:
        raise ContractError("private_control_reference_missing") from error


def recovery(repo, tx, project, command, previous, rows):
    if command.kind == "nested_begin":
        if not previous or previous["phase"] not in ("response_completed", "tool_running"):
            raise Conflict("private_nested_begin_requires_captured_parent")
        parent = previous["last_entry_id"]
        if parent not in rows or command.head.pending_parent_entry_id != parent:
            raise ContractError("private_nested_begin_return_point_missing")
    if command.kind != "authorized_replacement":
        return
    from podgen.runtime.evaluation.technical_recovery_policy import MAX_STAGE_REPLACEMENTS, VERSION

    authorization = command.data.get("authorization")
    if not isinstance(authorization, dict) or (
        authorization.get("kind") != VERSION
        or authorization.get("actor_id") != str(repo.actor_id)
        or authorization.get("maximum_new_attempts") != 1
        or type(authorization.get("maximum_stage_replacements")) is not int
        or not 1 <= authorization["maximum_stage_replacements"] <= MAX_STAGE_REPLACEMENTS
        or not authorization.get("evidence")
    ):
        raise ContractError("invalid_private_replacement_authorization")
    target = _parent(command, rows, "replacement_entry_id")
    if authorization.get("target_invocation_id") != str(target["attempt_id"]):
        raise ContractError("private_replacement_target_mismatch")
    stage = command.data.get("stage")
    if not stage:
        raise ContractError("private_replacement_stage_missing")
    count = query(
        tx,
        """SELECT count(*) FROM execution_entries
        WHERE project_id=:p AND run_id=:r AND checkpoint_kind='authorized_replacement'
        AND data->>'stage'=:stage""",
        p=project,
        r=command.run_id,
        stage=stage,
    ).scalar_one()
    if count >= authorization["maximum_stage_replacements"]:
        raise Conflict("private_replacement_limit_exceeded")


def authorize_parents(repo, tx, project, command, rows, *, privacy=True):
    for set_id in {row["input_set_id"] for row in rows.values()} - {command.input_set_id}:
        access.inputs(repo, tx, project, command.run_id, set_id)
    if privacy:
        access.conditions(tx, project, rows, ())
